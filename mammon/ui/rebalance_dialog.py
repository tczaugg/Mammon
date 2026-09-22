"""Target & Drift: the target asset mix, and how far the real one has strayed
(roadmap item 27 slice; SRD 5.8f, over :mod:`mammon.rebalance`).

A tree of asset classes -- target percent (editable in place), current percent,
the signed gap in points and relative terms, and the cents a rebalance would
move -- each expanding into the HOLDINGS that make it up, with what each has
gained since the target was last rebalanced. Out-of-band rows are colored,
because the whole reason for a band is that most deviations are noise and a few
are not.

Four things the user reported about the first version, and where each is answered
(2026-09-15):

* *"This mixes kinds of money. 401K + IRA shouldn't be mixed with ROTH."* The
  accounts belong to the target, and :func:`mammon.rebalance.set_target_accounts`
  refuses a set holding more than one tax treatment. The header says which kind
  of money is being measured.
* *"There is no customization for accounts. I wouldn't want to include the
  [529] accounts."* Accounts… opens a check-list; money held for someone else is
  simply unticked.
* *"Wouldn't it also make sense to show which assets within the asset class have
  changed the most?"* Each class expands into its holdings, sorted by value, with
  the change since the last rebalance -- the ones that moved are the ones that
  put the class off target. Trades stay the user's to choose: the window proposes
  none per holding.
* *"The difference between investments and cash and investments is unclear, as
  both have cash, yet the advice changes."* The sleeve picker is gone. Cash is in
  the mix when its account is ticked, and the header names the accounts.

Kept in its OWN module rather than as a fourth tab on the Allocation window:
this asks a different question ("is the mix where I meant it to be") from the
one that window answers ("where is my money"), it owns editable state the other
has none of, and a separate file keeps a large shared dialog module out of the
blast radius.

Nothing here writes a transaction. The Rebalance column is arithmetic -- what to
move to close the gap -- and executing it stays the user's job in the register.
The footer says so, and says the other thing the numbers cannot: a sale in a
taxable account realizes a gain, so the cheapest rebalance is usually the one
funded by new contributions rather than by selling the winner.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from mammon import rebalance
from mammon.ui import style
from mammon.ui.models import fmt_money

# Out-of-band colors by theme: (light, dark). Overweight reads as the
# "sell" direction and underweight as "buy", so they must not both be red.
_OVER = ("#b2382c", "#ff6b6b")
_UNDER = ("#1f5fa8", "#6fb1ff")

# Display verbs for rebalance.ClassDrift.action. Cash is Invest / Raise, never
# Sell -- see ClassDrift.action for why cash is spent and raised rather than
# sold -- and the unclassified bucket is never traded at all.
_MOVE_VERBS = {"buy": "Buy", "sell": "Sell", "invest": "Invest", "raise": "Raise",
               "classify": "Classify"}


def _dark() -> bool:
    return style.theme() == "dark"


def _pct(value: Decimal) -> str:
    return f"{value:.1f}"


def _signed_pct(value: Decimal) -> str:
    return f"{value:+.1f}"


class AccountPickerDialog(QDialog):
    """Which accounts a target governs. Grouped by the kind of money each holds,
    because a target may cover only one kind and the grouping is what makes an
    illegal selection obvious before it is refused."""

    def __init__(self, conn, chosen, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Accounts in this target")
        self.resize(460, 520)
        self.list = QListWidget()
        picked = {int(a) for a in chosen or ()}
        groups: dict = {}
        for aid, name, treatment in rebalance.accounts_for_picking(conn):
            groups.setdefault(treatment, []).append((aid, name))
        for treatment in list(rebalance.TAX_TREATMENTS) + [""]:
            rows = groups.get(treatment)
            if not rows:
                continue
            head = QListWidgetItem(rebalance.TAX_TREATMENT_LABELS[treatment])
            head.setFlags(Qt.ItemIsEnabled)
            font = QFont(head.font())
            font.setBold(True)
            head.setFont(font)
            self.list.addItem(head)
            for aid, name in rows:
                item = QListWidgetItem(f"    {name}")
                item.setData(Qt.UserRole, aid)
                item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                item.setCheckState(Qt.Checked if aid in picked else Qt.Unchecked)
                self.list.addItem(item)
        note = QLabel(
            "A target covers ONE kind of money: a Roth dollar and a 401(k) "
            "dollar are taxed differently and are rebalanced apart. Set an "
            "account's kind in Account details. Accounts held for someone else "
            "belong in their own target, or in none.")
        note.setObjectName("registerSub")
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.list, 1)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def chosen(self) -> list:
        out = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            aid = item.data(Qt.UserRole)
            if aid is not None and item.checkState() == Qt.Checked:
                out.append(int(aid))
        return out


class RebalanceDialog(QDialog):
    """Set a target mix and see the drift from it."""

    HEADERS = ["Asset class / holding", "Lock", "Target %", "Current %",
               "Drift (pts)", "Drift (rel)", "Current", "Target",
               "Rebalance", "Change"]
    (CLASS, LOCK, TARGET, CURRENT, DRIFT, DRIFT_REL, CUR_VAL, TGT_VAL,
     MOVE, CHANGE) = range(10)

    def __init__(self, conn, parent=None, as_of: Optional[str] = None):
        super().__init__(parent)
        self.conn = conn
        self.as_of = as_of
        self.report = None
        self._loading = False
        self._locked: set = set()
        self.setWindowTitle("Target & Drift")
        self.resize(1080, 640)

        self.target_combo = QComboBox()
        self.target_combo.currentIndexChanged.connect(self._on_target_chosen)
        self.new_btn = QPushButton("New…")
        self.new_btn.setAutoDefault(False)
        self.new_btn.setToolTip("Create a target mix, seeded from what you hold now.")
        self.new_btn.clicked.connect(lambda *_: self.new_target())
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setAutoDefault(False)
        self.delete_btn.clicked.connect(lambda *_: self.delete_target())

        top = QHBoxLayout()
        top.addWidget(QLabel("Target"))
        top.addWidget(self.target_combo, 1)
        top.addWidget(self.new_btn)
        top.addWidget(self.delete_btn)

        self.accounts_btn = QPushButton("Accounts…")
        self.accounts_btn.setAutoDefault(False)
        self.accounts_btn.setToolTip(
            "Choose the accounts this target governs. One kind of money per "
            "target; money held for others can be left out entirely.")
        # Lambdas, not the bound methods: a clicked signal hands a `checked`
        # bool to any slot that can take one, and choose_accounts would read it
        # as the chosen accounts -- clicking the button would EMPTY the target.
        self.accounts_btn.clicked.connect(lambda *_: self.choose_accounts())
        self.rebalanced_btn = QPushButton("Rebalanced today")
        self.rebalanced_btn.setAutoDefault(False)
        self.rebalanced_btn.setToolTip(
            "Mark the mix rebalanced now. Each holding's Change is measured from "
            "that date -- what has moved since you last acted is what put a class "
            "off target.")
        self.rebalanced_btn.clicked.connect(lambda *_: self.mark_rebalanced())
        self.band_abs = QDoubleSpinBox()
        self.band_abs.setRange(0.1, 100.0)
        self.band_abs.setSingleStep(0.5)
        self.band_abs.setSuffix(" pts")
        self.band_rel = QDoubleSpinBox()
        self.band_rel.setRange(0.1, 100.0)
        self.band_rel.setSingleStep(5.0)
        self.band_rel.setSuffix(" %")
        for box in (self.band_abs, self.band_rel):
            box.valueChanged.connect(self._on_band_changed)
        self.band_abs.setToolTip(
            "Act when a class is this many percentage POINTS from its target.")
        self.band_rel.setToolTip(
            "Act when a class is this far from its target as a percentage OF "
            "that target. Catches a small sleeve the points band never would.")

        bands = QHBoxLayout()
        bands.addWidget(self.accounts_btn)
        bands.addWidget(self.rebalanced_btn)
        bands.addSpacing(12)
        bands.addWidget(QLabel("Rebalance when off by"))
        bands.addWidget(self.band_abs)
        bands.addWidget(QLabel("or"))
        bands.addWidget(self.band_rel)
        bands.addStretch(1)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(self.HEADERS))
        self.tree.setHeaderLabels(self.HEADERS)
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setUniformRowHeights(False)
        hh = self.tree.header()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.CLASS, QHeaderView.Stretch)
        # The table kept its old name for the tests and callers that read it.
        self.table = self.tree

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.fixed_label = QLabel("")
        self.fixed_label.setObjectName("registerSub")
        self.fixed_label.setWordWrap(True)
        self.note = QLabel(
            "The Rebalance column is arithmetic, not an order: nothing here trades. "
            "A sale in a taxable account realizes a gain, so the cheapest way to "
            "close a gap is usually to point new contributions at whatever is "
            "underweight rather than to sell what is over.")
        self.note.setObjectName("registerSub")
        self.note.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(bands)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.fixed_label)
        layout.addWidget(self.note)
        layout.addWidget(buttons)

        self.reload_targets()

    # -- targets -----------------------------------------------------------
    def reload_targets(self) -> None:
        """Refill the target picker and select the active one."""
        self._loading = True
        try:
            self.target_combo.clear()
            targets = rebalance.list_targets(self.conn)
            for t in targets:
                self.target_combo.addItem(t["name"], int(t["id"]))
            active = rebalance.active_target(self.conn)
            if active is not None:
                i = self.target_combo.findData(int(active["id"]))
                if i >= 0:
                    self.target_combo.setCurrentIndex(i)
        finally:
            self._loading = False
        self.refresh()

    def current_target_id(self):
        return self.target_combo.currentData()

    def _on_target_chosen(self, *_) -> None:
        if self._loading:
            return
        tid = self.current_target_id()
        if tid is not None:
            # Choosing a target here IS making it the one in force; a picker that
            # showed one target while the rest of the app measured another would
            # be its own bug report.
            rebalance.set_active(self.conn, int(tid))
        self.refresh()

    def new_target(self) -> Optional[int]:
        """Create a target seeded from the mix held now -- the usual starting
        point, since the current mix is the one decision already made."""
        name = self._ask_name()
        if not name:
            return None
        try:
            tid = rebalance.target_from_current(self.conn, name, as_of=self.as_of,
                                                active=True)
        except ValueError as exc:
            # Most often: nothing classified yet, which is a next step, not a fault.
            self._warn("New target", str(exc))
            return None
        self.reload_targets()
        return tid

    def delete_target(self) -> None:
        tid = self.current_target_id()
        if tid is None:
            return
        name = self.target_combo.currentText()
        if not self._confirm("Delete target", f"Delete the target '{name}'?"):
            return
        rebalance.delete_target(self.conn, int(tid))
        remaining = rebalance.list_targets(self.conn)
        if remaining:
            rebalance.set_active(self.conn, int(remaining[0]["id"]))
        self.reload_targets()

    # -- accounts and the rebalance date ------------------------------------
    def choose_accounts(self, chosen=None) -> None:
        """Pick the accounts this target governs. ``chosen`` is the test seam:
        passing a list skips the dialog."""
        tid = self.current_target_id()
        if tid is None:
            return
        if chosen is None:
            chosen = self._ask_accounts(rebalance.target_accounts(self.conn, int(tid)))
        if chosen is None:
            return
        try:
            rebalance.set_target_accounts(self.conn, int(tid), chosen)
        except ValueError as exc:
            # One kind of money per target. Said here rather than prevented in
            # the picker: the reason is worth reading once.
            self._warn("Accounts in this target", str(exc))
            return
        self.refresh()

    def _ask_accounts(self, chosen):
        dlg = AccountPickerDialog(self.conn, chosen, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return None
        return dlg.chosen()

    def mark_rebalanced(self) -> None:
        tid = self.current_target_id()
        if tid is None:
            return
        rebalance.set_rebalanced(self.conn, int(tid))
        self.refresh()

    # -- seams tests override ----------------------------------------------
    def _ask_name(self) -> Optional[str]:
        name, ok = QInputDialog.getText(self, "New target", "Name for this mix:")
        return name.strip() if (ok and name.strip()) else None

    def _warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text,
                                    QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No) == QMessageBox.Yes

    # -- editing -----------------------------------------------------------
    def _on_band_changed(self, *_) -> None:
        if self._loading:
            return
        tid = self.current_target_id()
        if tid is None:
            return
        rebalance.update_target(self.conn, int(tid),
                                band_abs_pct=Decimal(str(self.band_abs.value())),
                                band_rel_pct=Decimal(str(self.band_rel.value())))
        self.refresh()

    def set_target_pct(self, asset_class: str, pct) -> None:
        """Set one class's target weight and redraw (the spin boxes call this).

        The numbers update NOW, but the tree is rebuilt on the next event-loop
        turn. This runs inside a spin box's own ``valueChanged``, and rebuilding
        the rows calls ``setItemWidget``, which DELETES that spin box while its
        signal frame is still live -- the heap-corruption pattern CLAUDE.md
        documents for ``setModelData`` (0xc0000374, no Python traceback). It is
        reachable in ordinary use: setting a class to 0 drops its target line,
        so the tree really is rebuilt on an ordinary edit.

        That zero no longer costs the class its ROW -- the editor asks drift for
        every class in the palette (``include_empty_classes``), so a zeroed one
        stays on screen at 0% and can be typed back in. It used to vanish, and
        with no way to bring it back the edit was one-way (reported).
        """
        tid = self.current_target_id()
        if tid is None or self._loading:
            return
        # The domain owns the arithmetic: it moves the OTHER unlocked classes so
        # the column still totals 100, and clamps an edit nothing can absorb.
        rebalance.set_line_balanced(self.conn, int(tid), asset_class,
                                    Decimal(str(pct)))
        self.report = rebalance.drift(self.conn, int(tid), as_of=self.as_of,
                                      include_empty_classes=True)
        self._fill_status(self.report)
        QTimer.singleShot(0, self._redraw_rows)

    def set_locked(self, asset_class: str, locked: bool) -> None:
        """Pin or release one class's weight (the lock checkboxes call this).

        Same deferral as ``set_target_pct`` and for the same reason: this runs
        inside the checkbox's own ``toggled``, and the redraw calls
        ``setItemWidget``, which would delete that checkbox under its live
        signal frame.
        """
        tid = self.current_target_id()
        if tid is None or self._loading:
            return
        rebalance.set_locked(self.conn, int(tid), asset_class, bool(locked))
        self.report = rebalance.drift(self.conn, int(tid), as_of=self.as_of,
                                      include_empty_classes=True)
        self._fill_status(self.report)
        QTimer.singleShot(0, self._redraw_rows)

    def _redraw_rows(self) -> None:
        """Rebuild the tree from the current report, off the signal stack."""
        if self.report is None:
            return
        self._loading = True
        try:
            self._fill_rows(self.report)
        finally:
            self._loading = False

    # -- drawing -----------------------------------------------------------
    def refresh(self) -> None:
        tid = self.current_target_id()
        self.delete_btn.setEnabled(tid is not None)
        self.accounts_btn.setEnabled(tid is not None)
        self.rebalanced_btn.setEnabled(tid is not None)
        if tid is None:
            self.tree.clear()
            self.report = None
            self.status.setText(
                "No target yet. New… builds one from the mix you hold today, "
                "which you can then edit.")
            self.fixed_label.setText("")
            return
        self.report = rebalance.drift(self.conn, int(tid), as_of=self.as_of,
                                      include_empty_classes=True)
        r = self.report
        self._loading = True
        try:
            self.band_abs.setValue(float(r.band_abs_pct))
            self.band_rel.setValue(float(r.band_rel_pct))
            self._fill_rows(r)
        finally:
            self._loading = False
        self._fill_status(r)

    def _fill_rows(self, r) -> None:
        idx = 1 if _dark() else 0
        tid = self.current_target_id()
        self._locked = (rebalance.locked_classes(self.conn, int(tid))
                        if tid is not None else set())
        self.tree.clear()
        for d in r.rows:
            color = None
            if d.out_of_band:
                color = _OVER[idx] if d.drift_pct > 0 else _UNDER[idx]
            item = QTreeWidgetItem(self.tree)
            item.setText(self.CLASS, d.label)
            item.setData(self.CLASS, Qt.UserRole, d.asset_class)
            if d.out_of_band:
                font = QFont(item.font(self.CLASS))
                font.setBold(True)
                item.setFont(self.CLASS, font)
                item.setFont(self.DRIFT, font)
                item.setFont(self.MOVE, font)
            item.setText(self.CURRENT, _pct(d.current_pct))
            item.setText(self.DRIFT, _signed_pct(d.drift_pct))
            rel = d.drift_rel_pct
            item.setText(self.DRIFT_REL, "—" if rel is None else _signed_pct(rel))
            item.setText(self.CUR_VAL, fmt_money(d.current_cents))
            item.setText(self.TGT_VAL, fmt_money(d.target_cents))
            # The verb comes from the DOMAIN (ClassDrift.action), not from the
            # sign of move_cents, so "cash is never sold" and "unclassified is
            # never traded" live in one place: cash reads Invest / Raise, and the
            # unclassified bucket reads Classify -- telling someone to sell what
            # is only missing an asset class is advice about the records.
            if d.action == "classify":
                move = "Classify these"
            elif d.action == "hold":
                move = "—"
            else:
                move = f"{_MOVE_VERBS[d.action]} {fmt_money(abs(d.move_cents))}"
            item.setText(self.MOVE, move)
            for col in (self.CURRENT, self.DRIFT, self.DRIFT_REL, self.CUR_VAL,
                        self.TGT_VAL, self.MOVE, self.CHANGE):
                item.setTextAlignment(col, Qt.AlignRight | Qt.AlignVCenter)
            if color:
                for col in (self.CLASS, self.DRIFT, self.DRIFT_REL, self.MOVE):
                    item.setForeground(col, QColor(color))
            self._fill_holdings(item, d, idx)
            # The widgets go on LAST: setItemWidget needs the item in the tree.
            locked = d.asset_class in self._locked
            if not d.is_unclassified:
                lock = QCheckBox()
                lock.setToolTip(
                    "Lock this weight. Locked classes are never moved when "
                    "another class is edited -- the unlocked ones absorb the "
                    "change so the column always totals 100%.")
                lock.setChecked(locked)
                lock.toggled.connect(
                    lambda on, cls=d.asset_class: self.set_locked(cls, on))
                holder = QWidget()
                box = QHBoxLayout(holder)
                box.setContentsMargins(0, 0, 0, 0)
                box.addStretch(1)
                box.addWidget(lock)
                box.addStretch(1)
                self.tree.setItemWidget(item, self.LOCK, holder)
                spin = QDoubleSpinBox()
                spin.setRange(0.0, 100.0)
                spin.setSingleStep(1.0)
                spin.setSuffix(" %")
                spin.setValue(float(d.target_pct))
                # A locked weight is not editable in place; unlock it first.
                spin.setEnabled(not locked)
                spin.valueChanged.connect(
                    lambda v, cls=d.asset_class: self.set_target_pct(cls, v))
                self.tree.setItemWidget(item, self.TARGET, spin)
            else:
                item.setText(self.TARGET, "—")

    def _fill_holdings(self, parent, d, idx) -> None:
        """The holdings inside one class: what a rebalance of it would trade,
        biggest first, with what each has done since the last rebalance."""
        for h in d.holdings:
            child = QTreeWidgetItem(parent)
            child.setText(self.CLASS, f"{h.symbol} — {h.account}")
            child.setText(self.CURRENT, _pct(h.pct_of_class))
            child.setText(self.CUR_VAL, fmt_money(h.value_cents))
            if not h.priced:
                child.setText(self.MOVE, "no price on record")
            if h.change_cents is not None:
                change = fmt_money(h.change_cents)
                if h.change_pct is not None:
                    change += f"  ({_signed_pct(h.change_pct)}%)"
                child.setText(self.CHANGE, change)
                if h.change_cents < 0:
                    child.setForeground(self.CHANGE, QColor(_UNDER[idx]))
            for col in (self.CURRENT, self.CUR_VAL, self.CHANGE):
                child.setTextAlignment(col, Qt.AlignRight | Qt.AlignVCenter)

    def _fill_status(self, r) -> None:
        parts = [rebalance.describe(r)]
        if not r.target_is_complete:
            # Shown, never normalized away: a target summing to 90 would
            # otherwise report drift the user could never clear.
            parts.append(f"Note: the target adds up to {_pct(r.target_total_pct)}%, "
                         f"not 100% — the numbers below are measured against it "
                         f"as entered.")
        span = ("since you marked it rebalanced on " if r.since_is_rebalance
                else "over the last year, until you mark it rebalanced — since ")
        parts.append(f"Change is {span}{r.since}.")
        self.status.setText("  ".join(parts))
        # Name the accounts being measured and the kind of money they hold. A
        # Cash row is the same word whether it is a brokerage's uninvested cash
        # or a bank balance, so the only way to tell a surprising figure from a
        # wrong one is to say what is in it.
        kind = rebalance.TAX_TREATMENT_LABELS.get(r.tax_treatment, "") if r.tax_treatment \
            else "no tax treatment set"
        covered = (f"{fmt_money(r.sleeve_total)} across "
                   f"{', '.join(r.sleeve_accounts) or 'no accounts'} ({kind}).")
        if not r.account_ids:
            covered += ("  These accounts were chosen by a rule, not by you — "
                        "use Accounts… to pick them.")
        if r.fixed_total:
            names = ", ".join(f"{label} {fmt_money(cents)}"
                              for label, cents in r.fixed_rows)
            covered += (f"  Not in the mix (nothing here can be rebalanced): "
                        f"{names}.")
        if r.unpriced:
            # Silence here valued a wallet's coins at nothing and let the whole
            # mix read as if they did not exist.
            covered += (f"\nNo price on record for {', '.join(sorted(set(r.unpriced)))}"
                        f" — counted as zero until one is.")
        if r.cash_only_accounts:
            total = sum(c for _, c in r.cash_only_accounts)
            who = ", ".join(n for n, _ in r.cash_only_accounts)
            covered += (f"\nNote: {fmt_money(total)} of the Cash line is the balance "
                        f"of accounts with no holdings recorded ({who}), so the "
                        f"Cash drift below is a records gap, not a decision.")
        self.fixed_label.setText(covered)

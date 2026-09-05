"""Target & Drift: the target asset mix, and how far the real one has strayed
(roadmap item 27 slice; SRD 5.8f, over :mod:`mammon.rebalance`).

A table of asset classes -- target percent (editable in place), current percent,
the signed gap in points and relative terms, and the cents a rebalance would
move. Out-of-band rows are coloured, because the whole reason for a band is that
most deviations are noise and a few are not.

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
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from mammon import rebalance
from mammon.ui import style
from mammon.ui.models import fmt_money

# Out-of-band colours by theme: (light, dark). Overweight reads as the
# "sell" direction and underweight as "buy", so they must not both be red.
_OVER = ("#b2382c", "#ff6b6b")
_UNDER = ("#1f5fa8", "#6fb1ff")

# Display verbs for rebalance.ClassDrift.action. Cash is Invest / Raise, never
# Sell -- see ClassDrift.action for why cash is spent and raised rather than sold.
_MOVE_VERBS = {"buy": "Buy", "sell": "Sell", "invest": "Invest", "raise": "Raise"}


def _dark() -> bool:
    return style.theme() == "dark"


def _item(text, align=None, color=None, bold=False) -> QTableWidgetItem:
    it = QTableWidgetItem("" if text is None else str(text))
    if align is not None:
        it.setTextAlignment(align | Qt.AlignVCenter)
    if color:
        it.setForeground(QColor(color))
    if bold:
        f = QFont(it.font())
        f.setBold(True)
        it.setFont(f)
    return it


def _pct(value: Decimal) -> str:
    return f"{value:.1f}"


def _signed_pct(value: Decimal) -> str:
    return f"{value:+.1f}"


class RebalanceDialog(QDialog):
    """Set a target mix and see the drift from it."""

    HEADERS = ["Asset class", "Target %", "Current %", "Drift (pts)",
               "Drift (rel)", "Current", "Target", "Rebalance"]
    CLASS, TARGET, CURRENT, DRIFT, DRIFT_REL, CUR_VAL, TGT_VAL, MOVE = range(8)

    def __init__(self, conn, parent=None, as_of: Optional[str] = None):
        super().__init__(parent)
        self.conn = conn
        self.as_of = as_of
        self.report = None
        self._loading = False
        self.setWindowTitle("Target & Drift")
        self.resize(920, 620)

        self.target_combo = QComboBox()
        self.target_combo.currentIndexChanged.connect(self._on_target_chosen)
        self.new_btn = QPushButton("New…")
        self.new_btn.setAutoDefault(False)
        self.new_btn.setToolTip("Create a target mix, seeded from what you hold now.")
        self.new_btn.clicked.connect(self.new_target)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setAutoDefault(False)
        self.delete_btn.clicked.connect(self.delete_target)

        top = QHBoxLayout()
        top.addWidget(QLabel("Target"))
        top.addWidget(self.target_combo, 1)
        top.addWidget(self.new_btn)
        top.addWidget(self.delete_btn)

        self.sleeve_combo = QComboBox()
        for key in rebalance.TARGET_SLEEVES:
            self.sleeve_combo.addItem(rebalance.SLEEVE_LABELS[key], key)
        self.sleeve_combo.setToolTip(
            "Which accounts the target governs. Property is never included -- "
            "nobody rebalances by selling 5% of a house.")
        self.sleeve_combo.currentIndexChanged.connect(self._on_sleeve_changed)
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
        bands.addWidget(QLabel("Accounts"))
        bands.addWidget(self.sleeve_combo, 1)
        bands.addSpacing(12)
        bands.addWidget(QLabel("Rebalance when off by"))
        bands.addWidget(self.band_abs)
        bands.addWidget(QLabel("or"))
        bands.addWidget(self.band_rel)
        bands.addStretch(1)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.CLASS, QHeaderView.Stretch)

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
        layout.addWidget(self.table, 1)
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
        sleeve = self.sleeve_combo.currentData() or "investments"
        try:
            tid = rebalance.target_from_current(self.conn, name, sleeve=sleeve,
                                                as_of=self.as_of, active=True)
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
    def _on_sleeve_changed(self, *_) -> None:
        if self._loading:
            return
        tid = self.current_target_id()
        if tid is None:
            return
        rebalance.update_target(self.conn, int(tid),
                                sleeve=self.sleeve_combo.currentData())
        self.refresh()

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

        The numbers update NOW, but the table is rebuilt on the next event-loop
        turn. This runs inside a spin box's own ``valueChanged``, and rebuilding
        the rows calls ``setCellWidget``, which DELETES that spin box while its
        signal frame is still live -- the heap-corruption pattern CLAUDE.md
        documents for ``setModelData`` (0xc0000374, no Python traceback). It is
        reachable in ordinary use: setting a class to 0 drops its target line,
        and a class with no holding then loses its row entirely.
        """
        tid = self.current_target_id()
        if tid is None:
            return
        rebalance.set_line(self.conn, int(tid), asset_class, Decimal(str(pct)))
        self.report = rebalance.drift(self.conn, int(tid), as_of=self.as_of)
        self._fill_status(self.report)
        QTimer.singleShot(0, self._redraw_rows)

    def _redraw_rows(self) -> None:
        """Rebuild the table from the current report, off the signal stack."""
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
        if tid is None:
            self.table.setRowCount(0)
            self.report = None
            self.status.setText(
                "No target yet. New… builds one from the mix you hold today, "
                "which you can then edit.")
            self.fixed_label.setText("")
            return
        self.report = rebalance.drift(self.conn, int(tid), as_of=self.as_of)
        r = self.report
        self._loading = True
        try:
            i = self.sleeve_combo.findData(r.sleeve)
            if i >= 0:
                self.sleeve_combo.setCurrentIndex(i)
            self.band_abs.setValue(float(r.band_abs_pct))
            self.band_rel.setValue(float(r.band_rel_pct))
            self._fill_rows(r)
        finally:
            self._loading = False
        self._fill_status(r)

    def _fill_rows(self, r) -> None:
        idx = 1 if _dark() else 0
        self.table.setRowCount(len(r.rows))
        for row, d in enumerate(r.rows):
            color = None
            if d.out_of_band:
                color = _OVER[idx] if d.drift_pct > 0 else _UNDER[idx]
            self.table.setItem(row, self.CLASS, _item(d.label, bold=d.out_of_band))
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 100.0)
            spin.setSingleStep(1.0)
            spin.setSuffix(" %")
            spin.setValue(float(d.target_pct))
            spin.valueChanged.connect(
                lambda v, cls=d.asset_class: self.set_target_pct(cls, v))
            self.table.setCellWidget(row, self.TARGET, spin)
            self.table.setItem(row, self.CURRENT,
                               _item(_pct(d.current_pct), Qt.AlignRight))
            self.table.setItem(row, self.DRIFT,
                               _item(_signed_pct(d.drift_pct), Qt.AlignRight,
                                     color, d.out_of_band))
            rel = d.drift_rel_pct
            self.table.setItem(row, self.DRIFT_REL,
                               _item("—" if rel is None else _signed_pct(rel),
                                     Qt.AlignRight, color))
            self.table.setItem(row, self.CUR_VAL,
                               _item(fmt_money(d.current_cents), Qt.AlignRight))
            self.table.setItem(row, self.TGT_VAL,
                               _item(fmt_money(d.target_cents), Qt.AlignRight))
            # The verb comes from the DOMAIN (ClassDrift.action), not from the
            # sign of move_cents, so the "cash is never sold" rule lives in one
            # place: cash reads Invest / Raise, never Sell (it is spent and
            # raised as the by-product of the securities trades).
            self.table.setItem(
                row, self.MOVE,
                _item("—" if d.action == "hold" else
                      f"{_MOVE_VERBS[d.action]} {fmt_money(abs(d.move_cents))}",
                      Qt.AlignRight, color, d.out_of_band))

    def _fill_status(self, r) -> None:
        parts = [rebalance.describe(r)]
        if not r.target_is_complete:
            # Shown, never normalized away: a target summing to 90 would
            # otherwise report drift the user could never clear.
            parts.append(f"Note: the target adds up to {_pct(r.target_total_pct)}%, "
                         f"not 100% — the numbers below are measured against it "
                         f"as entered.")
        self.status.setText("  ".join(parts))
        # Name the accounts in the sleeve. A Cash row is the same word whether
        # it is a brokerage's uninvested cash (which belongs) or a bank balance
        # (which does not, under an investments-only sleeve), so the only way to
        # tell a surprising figure from a wrong one is to say what is in it.
        covered = (f"Sleeve {fmt_money(r.sleeve_total)} over "
                   f"{', '.join(r.sleeve_accounts) or 'no accounts'}.")
        if r.fixed_total:
            names = ", ".join(f"{label} {fmt_money(cents)}"
                              for label, cents in r.fixed_rows)
            covered += (f"  Not in the mix (nothing here can be rebalanced): "
                        f"{names}.")
        if r.cash_only_accounts:
            total = sum(c for _, c in r.cash_only_accounts)
            who = ", ".join(n for n, _ in r.cash_only_accounts)
            covered += (f"\nNote: {fmt_money(total)} of the Cash line is the balance "
                        f"of accounts with no holdings recorded ({who}), so the "
                        f"Cash drift below is a records gap, not a decision.")
        self.fixed_label.setText(covered)

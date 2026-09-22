"""Rebalancing stated per FUND, with the whole picture underneath (SRD 5.8f-1).

Why this is not the Target & Drift dialog
-----------------------------------------
That one states a weight per ASSET CLASS, which is not something you can trade.
A blended fund moves three classes at once, so "sell $30,000 of domestic stock"
has to be decomposed across holdings whose mixes differ -- and the user ruled
out solving for it: "now we are becoming too prescriptive. So I don't want to
go there."

Here the weights are per fund inside an account, which is the user's own career
practice and what a broker's auto-rebalance actually executes. The instruction
becomes "FXAIX is 22%, target 25%, buy 3%". The class mix stops being an
instruction and becomes the two bars at the bottom.

Expanding an account IS selecting it
------------------------------------
The one interaction worth explaining. Every investment account is listed with
its own composition bar; open one and its target joins the projection. So with
everything closed the two bottom bars are identical, and each account you open
moves the lower one. That is the reported design, and it avoids a second
selection control saying the same thing: the row you are working on is the row
whose target counts.

It also answers the question that made this worth building -- a target set in
one account says nothing about what the other accounts drifted to -- because
the lower bar is over EVERYTHING owned, not just the accounts in play.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mammon import ledger, portfolio, rebalance
from mammon.ui.asset_allocation import (
    ClassBar,
    ClassLegend,
    UNCLASSIFIED,
)
from mammon.ui.models import fmt_money

#: Columns. The bar is widest because it is the thing being read; the four
#: number columns are narrow because each holds a short figure.
(COL_NAME, COL_BAR, COL_CURRENT, COL_TARGET, COL_DRIFT, COL_MOVE) = range(6)
HEADERS = ["Account / fund", "Composition", "In account", "Target",
           "Drift", "Buy / Sell"]
COL_WIDTHS = {COL_NAME: 230, COL_CURRENT: 90, COL_TARGET: 90,
              COL_DRIFT: 80, COL_MOVE: 110}

#: Height of the two summary bars at the foot. Taller than a row's bar because
#: they carry their percentages as text.
SUMMARY_BAR_HEIGHT = 26


def _pct(value) -> str:
    return f"{float(value):.1f}%"


def _signed_pct(value) -> str:
    return f"{float(value):+.1f} pp"


class FundTargetWindow(QDialog):
    """Accounts with their composition, expandable into funds you can target."""

    changed = pyqtSignal()

    def __init__(self, conn, parent=None, *, target_id: Optional[int] = None,
                 as_of: Optional[str] = None):
        super().__init__(parent)
        self.conn = conn
        self.as_of = as_of
        self._target_id = target_id
        self._loading = False
        self.setWindowTitle("Rebalance by fund")
        self.resize(1040, 700)

        outer = QVBoxLayout(self)
        self.intro = QLabel(
            "Open an account to set a target percent for each fund in it. "
            "An open account's target counts toward the lower bar; a closed "
            "one is left exactly as it is.", self)
        self.intro.setWordWrap(True)
        outer.addWidget(self.intro)

        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(len(HEADERS))
        self.tree.setHeaderLabels(HEADERS)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.tree.header()
        for col, width in COL_WIDTHS.items():
            header.setSectionResizeMode(col, QHeaderView.Interactive)
            header.resizeSection(col, width)
        header.setSectionResizeMode(COL_BAR, QHeaderView.Stretch)
        # Expanding is the selection, so both directions have to recompute.
        self.tree.itemExpanded.connect(self._on_expansion_changed)
        self.tree.itemCollapsed.connect(self._on_expansion_changed)
        outer.addWidget(self.tree, 1)

        self.legend = ClassLegend(self)
        outer.addWidget(self.legend)

        self.current_label = QLabel("Everything you own, now", self)
        outer.addWidget(self.current_label)
        self.current_bar = ClassBar(parent=self, show_labels=True,
                                    height=SUMMARY_BAR_HEIGHT)
        outer.addWidget(self.current_bar)

        self.target_label = QLabel("...with the open accounts at their targets",
                                   self)
        outer.addWidget(self.target_label)
        self.target_bar = ClassBar(parent=self, show_labels=True,
                                   height=SUMMARY_BAR_HEIGHT)
        outer.addWidget(self.target_bar)

        self.note = QLabel("", self)
        self.note.setWordWrap(True)
        outer.addWidget(self.note)

        row = QHBoxLayout()
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        outer.addLayout(row)

        self._expanded: set = set()
        self.reload()

    # -- data ---------------------------------------------------------------
    #: The target this window creates when the ledger has none. Named, not
    #: anonymous, so a second window finds the SAME one: the first draft looked
    #: for the ACTIVE target, and `create_target` does not activate what it
    #: makes -- so every open made a fresh empty target and the weights typed
    #: last time were silently gone (caught by rendering it: the lower bar read
    #: 67% cash, every fund weight having defaulted to zero).
    DEFAULT_TARGET_NAME = "Fund targets"

    def target_id(self) -> Optional[int]:
        """The target whose fund weights are being edited, created on demand so
        the window is usable without a setup step.

        Preference order: one already asked for, then any target that HAS fund
        lines (this window's own kind), then the active one, then a new one.
        """
        if self._target_id is not None:
            return self._target_id
        for row in rebalance.list_targets(self.conn):
            if rebalance.has_fund_lines(self.conn, int(row["id"])):
                self._target_id = int(row["id"])
                return self._target_id
        for row in rebalance.list_targets(self.conn):
            if row["name"] == self.DEFAULT_TARGET_NAME:
                self._target_id = int(row["id"])
                return self._target_id
        active = rebalance.active_target(self.conn)
        if active is not None:
            self._target_id = int(active["id"])
        else:
            self._target_id = int(rebalance.create_target(
                self.conn, self.DEFAULT_TARGET_NAME))
        return self._target_id

    def expanded_accounts(self) -> list:
        """The accounts whose targets are in play -- the open ones."""
        return sorted(self._expanded)

    def report(self):
        return rebalance.fund_target(self.conn, self.target_id(), self.as_of,
                                     account_ids=self.expanded_accounts())

    def _account_rows(self) -> list:
        """``[(account_id, name, total, {class: cents}, [holding...])]``, biggest
        account first."""
        from mammon import security_mix
        mixtures = security_mix.all_mixtures(self.conn)
        classes = {r["symbol"]: r["asset_class"]
                   for r in portfolio.list_securities(self.conn)}
        out = []
        for aid in portfolio.scope_account_ids(self.conn, "investments"):
            val = portfolio.account_valuation(self.conn, aid, self.as_of)
            acct = ledger.get_account(self.conn, aid)
            name = acct["name"] if acct is not None else str(aid)
            weights: dict = {}
            holdings = []
            for h in val.holdings:
                if h.price is None:
                    continue
                cents = int(h.market_value)
                holdings.append((h.symbol, cents))
                for cls, part in rebalance._class_split(
                        self.conn, h.symbol, cents, mixtures, classes).items():
                    weights[cls] = weights.get(cls, 0) + part
            if val.cash:
                weights["cash"] = weights.get("cash", 0) + int(val.cash)
            total = int(val.total)
            if total <= 0:
                continue
            out.append((aid, name, total, weights,
                        sorted(holdings, key=lambda h: -h[1])))
        return sorted(out, key=lambda r: -r[2])

    # -- rendering ----------------------------------------------------------
    def reload(self) -> None:
        self._loading = True
        try:
            self._fill_tree()
            self._fill_summary()
        finally:
            self._loading = False

    def _fill_tree(self) -> None:
        self.tree.clear()
        lines = rebalance.fund_lines(self.conn, self.target_id())
        for aid, name, total, weights, holdings in self._account_rows():
            item = QTreeWidgetItem(self.tree)
            item.setData(COL_NAME, Qt.UserRole, ("account", aid))
            item.setText(COL_NAME, name)
            item.setText(COL_MOVE, fmt_money(total))
            item.setTextAlignment(COL_MOVE, Qt.AlignRight | Qt.AlignVCenter)
            bar = ClassBar(weights, self.tree)
            self.tree.setItemWidget(item, COL_BAR, bar)

            named = sorted({sym for (a, sym) in lines if a == aid}
                           | {sym for sym, _c in holdings})
            held = dict(holdings)
            for symbol in named:
                cents = held.get(symbol, 0)
                kid = QTreeWidgetItem(item)
                kid.setData(COL_NAME, Qt.UserRole, ("fund", aid, symbol))
                kid.setText(COL_NAME, symbol)
                kid.setText(COL_CURRENT,
                            _pct(Decimal(cents) / Decimal(total) * 100)
                            if total else "")
                kid.setTextAlignment(COL_CURRENT, Qt.AlignRight | Qt.AlignVCenter)
                for col in (COL_TARGET, COL_DRIFT, COL_MOVE):
                    kid.setTextAlignment(col, Qt.AlignRight | Qt.AlignVCenter)
            # The spin boxes go on after the children are in the tree.
            for i in range(item.childCount()):
                kid = item.child(i)
                _kind, acct_id, symbol = kid.data(COL_NAME, Qt.UserRole)
                spin = QDoubleSpinBox(self.tree)
                spin.setRange(0.0, 100.0)
                spin.setDecimals(1)
                spin.setSuffix(" %")
                spin.setValue(float(lines.get((acct_id, symbol), 0)))
                spin.valueChanged.connect(
                    lambda value, a=acct_id, s=symbol: self.set_fund_pct(a, s, value))
                self.tree.setItemWidget(kid, COL_TARGET, spin)
            if aid in self._expanded:
                item.setExpanded(True)
        self._fill_fund_numbers()

    def _fill_fund_numbers(self) -> None:
        """Drift and buy/sell on the fund rows of OPEN accounts.

        A closed account's target is not in play, so its funds show no drift
        and no trade -- the numbers would be describing a projection the lower
        bar is not making."""
        rep = self.report()
        by_key = {(f.account_id, f.symbol): f for f in rep.funds}
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                kid = top.child(j)
                _kind, aid, symbol = kid.data(COL_NAME, Qt.UserRole)
                fund = by_key.get((aid, symbol))
                if fund is None:
                    kid.setText(COL_DRIFT, "")
                    kid.setText(COL_MOVE, "")
                    continue
                kid.setText(COL_DRIFT, _signed_pct(fund.drift_pct))
                kid.setText(COL_MOVE,
                            "—" if fund.action == "hold"
                            else f"{'Buy' if fund.move_cents > 0 else 'Sell'} "
                                 f"{fmt_money(abs(fund.move_cents))}")

    def _fill_summary(self) -> None:
        rep = self.report()
        self.current_bar.set_weights(rep.portfolio_before)
        self.target_bar.set_weights(rep.portfolio_after)
        classes = [c for c in portfolio.ASSET_CLASSES
                   if rep.portfolio_before.get(c) or rep.portfolio_after.get(c)]
        if rep.portfolio_before.get(UNCLASSIFIED) or rep.portfolio_after.get(UNCLASSIFIED):
            classes.append(UNCLASSIFIED)
        self.legend.set_classes(classes)

        open_names = [ledger.get_account(self.conn, a)["name"]
                      for a in self.expanded_accounts()
                      if ledger.get_account(self.conn, a) is not None]
        if not open_names:
            self.target_label.setText(
                "...with the open accounts at their targets "
                "(none open, so the bars match)")
            self.note.setText("")
        else:
            self.target_label.setText(
                "...with %s at their targets" % ", ".join(open_names))
            incomplete = rep.accounts_complete
            if incomplete:
                names = ", ".join(
                    f"{ledger.get_account(self.conn, a)['name']} ({float(total):.1f}%)"
                    for a, total in incomplete
                    if ledger.get_account(self.conn, a) is not None)
                self.note.setText(
                    f"Weights do not add to 100% in: {names}. The shortfall is "
                    f"left as cash rather than scaled into the funds.")
            else:
                self.note.setText("")

    # -- interaction --------------------------------------------------------
    def _on_expansion_changed(self, item) -> None:
        if self._loading:
            return
        subject = item.data(COL_NAME, Qt.UserRole)
        if not subject or subject[0] != "account":
            return
        aid = int(subject[1])
        if item.isExpanded():
            self._expanded.add(aid)
        else:
            self._expanded.discard(aid)
        self._fill_fund_numbers()
        self._fill_summary()

    def set_fund_pct(self, account_id: int, symbol: str, pct) -> None:
        """Store one fund's target weight and redraw the numbers that depend on
        it. The tree is NOT rebuilt: this runs inside a spin box's own
        valueChanged, and rebuilding calls setItemWidget, which would delete
        that spin box under its live signal frame -- the heap-corruption
        pattern CLAUDE.md documents."""
        if self._loading:
            return
        rebalance.set_fund_line(self.conn, self.target_id(), int(account_id),
                                symbol, Decimal(str(pct)))
        self._fill_fund_numbers()
        self._fill_summary()
        self.changed.emit()

    # -- seams for tests ----------------------------------------------------
    def expand_account(self, account_id: int, expanded: bool = True) -> None:
        """Programmatic equivalent of clicking the expander."""
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            subject = top.data(COL_NAME, Qt.UserRole)
            if subject and subject[0] == "account" and int(subject[1]) == int(account_id):
                top.setExpanded(bool(expanded))
                return
        raise AssertionError(f"no account row for {account_id}")

    def account_bar(self, account_id: int) -> Optional[ClassBar]:
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            subject = top.data(COL_NAME, Qt.UserRole)
            if subject and subject[0] == "account" and int(subject[1]) == int(account_id):
                return self.tree.itemWidget(top, COL_BAR)
        return None

    def fund_row_text(self, account_id: int, symbol: str) -> tuple:
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                kid = top.child(j)
                _kind, aid, sym = kid.data(COL_NAME, Qt.UserRole)
                if aid == account_id and sym == symbol:
                    return (kid.text(COL_CURRENT), kid.text(COL_DRIFT),
                            kid.text(COL_MOVE))
        raise AssertionError(f"no fund row for {account_id}/{symbol}")

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

Opening an account offers a target it does not have
---------------------------------------------------
Every fund starts at zero, which is the one weight that is certainly wrong, and
typing eight of them from memory is how a target never gets set at all. So the
first time an account is opened, :func:`rebalance.suggest_fund_lines` reads the
last mix the user actually stated -- a reallocation, or how they split money
going in -- and fills the spin boxes with it, saying underneath where it came
from. It is a starting point, not a recommendation: every figure was the user's
own, and editing one is the same keystroke it was before.

An account already carrying weights is never re-seeded. Suggesting over a
statement the user typed themselves would be the one way this feature could
destroy something.

Which accounts are "everything you own"
---------------------------------------
The gear is the app's usual customization popup, restricted to investment
accounts, and it narrows BOTH lists: the accounts offered for rebalancing and
the two bars at the foot. Money held for someone else is not part of the mix at
all -- "I wouldn't want to include the [529] accounts here as those are for my
kids" -- and no scope rule can know that, only the person can.
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
from mammon.ui import prefs
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
        head = QHBoxLayout()
        self.intro = QLabel(
            "Open an account to set a target percent for each fund in it. "
            "An open account's target counts toward the lower bar; a closed "
            "one is left exactly as it is.", self)
        self.intro.setWordWrap(True)
        head.addWidget(self.intro, 1)
        self._account_scope = prefs.rebalance_account_scope(
            prefs.ledger_path(self.conn))
        self._build_gear(head)
        outer.addLayout(head)

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
        close = buttons.button(QDialogButtonBox.Close)
        if close is not None:
            # Reported: "If I hit Enter while editing a target amount the dialog
            # should not close." A QDialogButtonBox promotes its button to the
            # dialog default, and a spin box IGNORES Return once it has read the
            # typed value -- so the keystroke that means "I have finished this
            # number" arrived at Close as a click. Both halves are turned off;
            # `keyPressEvent` then stops the key for good.
            close.setAutoDefault(False)
            close.setDefault(False)
        row.addWidget(buttons)
        outer.addLayout(row)

        self._expanded: set = set()
        #: What each account's targets were seeded FROM, for the note under the
        #: bars. Keyed by account id; an account the user has typed into is
        #: absent, because nothing was suggested for it.
        self._seeds: dict = {}
        self.reload()

    def keyPressEvent(self, event) -> None:
        """Swallow Return/Enter so no amount of typing can close the window.

        Every control here is an editor, and the dialog has nothing to accept:
        weights are stored as they are typed. Closing is a click on Close."""
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)

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

    def _build_gear(self, row) -> None:
        """The accounts gear: the app's existing ``CustomizeDialog`` restricted
        to investment accounts, exactly as the Investment Dashboard builds it,
        so the list and the way it is remembered are ones the user knows.

        Its own remembered scope, though. The dashboard asks "what am I looking
        at" and this asks "what is mine to rebalance"."""
        from mammon.ui.report_filters import (
            CustomizeDialog, customize_button, set_account_picker_ids)
        self.customize_dialog = CustomizeDialog(
            self.conn, self.as_of, self.as_of, show_accounts=True,
            account_types=ledger.INVESTMENT_LIKE_TYPES, parent=self)
        picker = self.customize_dialog.filters.account_list
        if self._account_scope is not None and picker is not None:
            set_account_picker_ids(picker, self._account_scope)
        self.customize_dialog.applied.connect(self._on_accounts_customized)
        self.gear = customize_button(self.customize_dialog, parent=self)
        self.gear.setToolTip("Choose which accounts this window covers")
        row.addWidget(self.gear, 0, Qt.AlignTop)

    def _on_accounts_customized(self) -> None:
        self.set_account_scope(self.customize_dialog.filters.selected_account_ids())
        prefs.set_rebalance_account_scope(prefs.ledger_path(self.conn),
                                          self.account_scope())

    def account_scope(self) -> Optional[list]:
        """The chosen accounts, or None for "every investment account" -- the
        same None-means-no-filter contract ``ReportFilterBar`` uses."""
        return None if self._account_scope is None else list(self._account_scope)

    def set_account_scope(self, account_ids) -> None:
        """Narrow both the account list and the two summary bars.

        An account that leaves the scope also leaves the projection: its target
        cannot count toward a picture it is no longer part of."""
        if account_ids is None:
            self._account_scope = None
        else:
            allowed = set(portfolio.scope_account_ids(self.conn, "investments"))
            self._account_scope = [int(a) for a in account_ids if int(a) in allowed]
            self._expanded &= set(self._account_scope)
        self.reload()

    def scoped_account_ids(self) -> list:
        """The investment accounts this window is working over."""
        every = portfolio.scope_account_ids(self.conn, "investments")
        if self._account_scope is None:
            return list(every)
        keep = set(self._account_scope)
        return [aid for aid in every if aid in keep]

    def expanded_accounts(self) -> list:
        """The accounts whose targets are in play -- the open ones."""
        return sorted(self._expanded)

    def report(self):
        return rebalance.fund_target(self.conn, self.target_id(), self.as_of,
                                     account_ids=self.expanded_accounts(),
                                     scope_ids=self.scoped_account_ids())

    def _account_rows(self) -> list:
        """``[(account_id, name, total, {class: cents}, [holding...])]``, biggest
        account first."""
        from mammon import security_mix
        mixtures = security_mix.all_mixtures(self.conn)
        classes = {r["symbol"]: r["asset_class"]
                   for r in portfolio.list_securities(self.conn)}
        out = []
        for aid in self.scoped_account_ids():
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
            return
        self.target_label.setText(
            "...with %s at their targets" % ", ".join(open_names))
        notes = []
        incomplete = rep.accounts_complete
        if incomplete:
            names = ", ".join(
                f"{ledger.get_account(self.conn, a)['name']} ({float(total):.1f}%)"
                for a, total in incomplete
                if ledger.get_account(self.conn, a) is not None)
            notes.append(
                f"Weights do not add to 100% in: {names}. The shortfall is "
                f"left as cash rather than scaled into the funds.")
        for aid in self.expanded_accounts():
            seed = self._seeds.get(aid)
            acct = ledger.get_account(self.conn, aid)
            if seed is not None and acct is not None:
                notes.append(f"{acct['name']}: {seed.describe()}")
        self.note.setText("  ".join(notes))

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
            self._seed_account(aid)
        else:
            self._expanded.discard(aid)
        self._fill_fund_numbers()
        self._fill_summary()

    def _seed_account(self, account_id: int) -> None:
        """Fill an untargeted account with the last mix it was stated to hold.

        Only when it carries no weight at all. Suggesting over something the
        user typed would be the one way this could destroy their own work, and
        "I set that to zero deliberately" is indistinguishable from "I never
        set it" only if you ignore the other funds in the account.
        """
        tid = self.target_id()
        if any(aid == account_id for (aid, _sym) in
               rebalance.fund_lines(self.conn, tid)):
            return
        seed = rebalance.suggest_fund_lines(self.conn, account_id, self.as_of)
        if seed is None:
            return
        for symbol, pct in seed.lines.items():
            rebalance.set_fund_line(self.conn, tid, account_id, symbol, pct)
        self._seeds[account_id] = seed
        self._write_spins(account_id, seed.lines)

    def _write_spins(self, account_id: int, lines: dict) -> None:
        """Push weights into the spin boxes of one account, in place.

        The tree is NOT rebuilt and no widget is replaced: this runs inside
        ``itemExpanded``, and ``setItemWidget`` there would delete a widget
        under a live signal frame -- the heap-corruption pattern CLAUDE.md
        documents. ``_loading`` keeps each ``setValue`` from being stored right
        back again through ``valueChanged``."""
        was, self._loading = self._loading, True
        try:
            for i in range(self.tree.topLevelItemCount()):
                top = self.tree.topLevelItem(i)
                for j in range(top.childCount()):
                    kid = top.child(j)
                    _kind, aid, symbol = kid.data(COL_NAME, Qt.UserRole)
                    if int(aid) != int(account_id):
                        continue
                    spin = self.tree.itemWidget(kid, COL_TARGET)
                    if spin is not None:
                        spin.setValue(float(lines.get(symbol, 0)))
        finally:
            self._loading = was

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

    def fund_target_pct(self, account_id: int, symbol: str) -> float:
        """What one fund's target spin box currently shows."""
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                kid = top.child(j)
                _kind, aid, sym = kid.data(COL_NAME, Qt.UserRole)
                if aid == account_id and sym == symbol:
                    spin = self.tree.itemWidget(kid, COL_TARGET)
                    return float(spin.value()) if spin is not None else 0.0
        raise AssertionError(f"no fund row for {account_id}/{symbol}")

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

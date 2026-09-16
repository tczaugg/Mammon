"""Lots, Capital Gains, Performance, Allocation and Specify Lots (roadmap item
7): the investment register's windows over :mod:`mammon.portfolio`.

Each is a thin table over one domain call, valued as of the ledger's last
activity unless the user picks a date, and none of them writes anything except
through the three seams the domain offers -- a security's asset class
(``portfolio.set_security``), an account's own asset class
(``portfolio.set_account_asset_class``) and the lots named for a sale
(``investments.assign_lots``). Nothing here exec_()s another modal on its own;
errors go through an overridable ``_warn`` so the windows stay testable
headless.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from mammon import investments, ledger, portfolio, security_mix
from mammon.ui import prefs, style
from mammon.ui.delegates import date_edit_iso, make_date_edit
from mammon.ui.models import fmt_date, fmt_money

METHOD_LABELS = {"average": "Average cost", "fifo": "First in, first out",
                 "lifo": "Last in, first out"}
TERM_LABELS = {"long": "Long", "short": "Short", "unknown": "?"}


def _item(text, align=None) -> QTableWidgetItem:
    it = QTableWidgetItem("" if text is None else str(text))
    if align is not None:
        it.setTextAlignment(align | Qt.AlignVCenter)
    return it


def _money(cents: Optional[int]) -> QTableWidgetItem:
    it = _item("" if cents is None else fmt_money(cents), Qt.AlignRight)
    if cents is not None and cents < 0:
        it.setForeground(QColor(style.negative_color()))
    return it


def _table(headers, stretch_col=0) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    hh = t.horizontalHeader()
    hh.setSectionResizeMode(QHeaderView.ResizeToContents)
    hh.setSectionResizeMode(stretch_col, QHeaderView.Stretch)
    return t


def _fill(table: QTableWidget, rows) -> None:
    table.setRowCount(len(rows))
    for r, cells in enumerate(rows):
        for c, cell in enumerate(cells):
            table.setItem(r, c, cell if isinstance(cell, QTableWidgetItem) else _item(cell))


def _symbols(conn, account_id) -> list:
    return [h["symbol"] for h in investments.list_holdings(conn, account_id)]


def _default_as_of(conn) -> str:
    return investments.valuation_as_of(conn) or _dt.date.today().isoformat()


# ---------------------------------------------------------------------------
# Lots
# ---------------------------------------------------------------------------
class LotsDialog(QDialog):
    """Every open lot in the account: when those shares were bought, what
    they cost, what they are worth now, and whether selling them would be a
    long- or short-term gain. The cost the register's Holdings window sums."""
    HEADERS = ["Security", "Acquired", "Shares", "Cost", "Price", "Market Value",
               "Gain/Loss", "Term", "Days"]

    def __init__(self, conn, account_id, parent=None, as_of: Optional[str] = None):
        super().__init__(parent)
        self.conn, self.account_id = conn, int(account_id)
        acct = ledger.get_account(conn, account_id)
        name = acct["name"] if acct else ""
        self.as_of = as_of or _default_as_of(conn)
        self.setWindowTitle(f"Lots - {name}")
        self.resize(860, 480)
        method = investments.get_lot_method(conn, account_id)
        self.method_label = QLabel(
            f"Cost basis: {METHOD_LABELS.get(method, method)} (set in Account Details). "
            f"Valued as of {fmt_date(self.as_of)}.")
        self.method_label.setWordWrap(True)
        self.symbol = QComboBox()
        self.symbol.addItem("All securities", None)
        for s in _symbols(conn, account_id):
            self.symbol.addItem(s, s)
        self.symbol.currentIndexChanged.connect(lambda *_: self.reload())
        self.table = _table(self.HEADERS)
        self.footer = QLabel()
        top = QHBoxLayout()
        top.addWidget(QLabel("Security"))
        top.addWidget(self.symbol)
        top.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addWidget(self.method_label)
        layout.addLayout(top)
        layout.addWidget(self.table)
        layout.addWidget(self.footer)
        layout.addWidget(buttons)
        self.reload()

    def reload(self) -> None:
        self.lots = portfolio.open_lots(self.conn, self.account_id,
                                        symbol=self.symbol.currentData(), as_of=self.as_of)
        _fill(self.table, [[
            l.symbol, fmt_date(l.acquired) if l.acquired else "(unknown)",
            _item(str(l.quantity), Qt.AlignRight), _money(l.cost),
            _item("" if l.price is None else str(l.price), Qt.AlignRight),
            _money(l.market_value if l.price is not None else None), _money(l.gain),
            TERM_LABELS.get(l.term, l.term),
            _item("" if l.days_held is None else str(l.days_held), Qt.AlignRight)]
            for l in self.lots])
        cost = sum(l.cost for l in self.lots)
        priced = [l for l in self.lots if l.price is not None]
        value = sum(l.market_value for l in priced)
        gain = sum(l.gain for l in priced)
        n_unpriced = len(self.lots) - len(priced)
        text = (f"{len(self.lots)} lot{'s' if len(self.lots) != 1 else ''}    "
                f"Cost {fmt_money(cost)}    Market value {fmt_money(value)}    "
                f"Gain/Loss {fmt_money(gain)}")
        if n_unpriced:
            text += f"    ({n_unpriced} unpriced, not in value or gain)"
        self.footer.setText(text)


# ---------------------------------------------------------------------------
# Capital Gains
# ---------------------------------------------------------------------------
class CapitalGainsDialog(QDialog):
    """Realized gains and losses from sales in a date range, one row per lot
    sold, with the short-/long-term totals a Schedule D wants."""
    HEADERS = ["Security", "Acquired", "Sold", "Shares", "Proceeds", "Basis",
               "Gain/Loss", "Term"]

    def __init__(self, conn, account_id, parent=None, year: Optional[int] = None):
        super().__init__(parent)
        self.conn, self.account_id = conn, int(account_id)
        acct = ledger.get_account(conn, account_id)
        name = acct["name"] if acct else ""
        self.setWindowTitle(f"Capital Gains - {name}")
        self.resize(860, 480)
        method = investments.get_lot_method(conn, account_id)
        note = QLabel(f"Cost basis: {METHOD_LABELS.get(method, method)}. A sale's lots "
                      "can be named on its row (right-click, Specify Lots).")
        note.setWordWrap(True)
        if year is None:
            year = int(_default_as_of(conn)[:4])
        self.year = QSpinBox()
        self.year.setRange(1900, 2200)
        self.year.setValue(year)
        self.start = make_date_edit(self, f"{year}-01-01")
        self.end = make_date_edit(self, f"{year}-12-31")
        self.year.valueChanged.connect(self._year_changed)
        self.start.dateChanged.connect(lambda *_: self.reload())
        self.end.dateChanged.connect(lambda *_: self.reload())
        self.table = _table(self.HEADERS)
        self.footer = QLabel()
        self.footer.setWordWrap(True)
        top = QHBoxLayout()
        top.addWidget(QLabel("Year"))
        top.addWidget(self.year)
        top.addSpacing(12)
        top.addWidget(QLabel("From"))
        top.addWidget(self.start)
        top.addWidget(QLabel("to"))
        top.addWidget(self.end)
        top.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addLayout(top)
        layout.addWidget(self.table)
        layout.addWidget(self.footer)
        layout.addWidget(buttons)
        self.reload()

    def _year_changed(self, year: int) -> None:
        self.start.blockSignals(True)
        self.end.blockSignals(True)
        self.start.setDate(self.start.date().__class__(year, 1, 1))
        self.end.setDate(self.end.date().__class__(year, 12, 31))
        self.start.blockSignals(False)
        self.end.blockSignals(False)
        self.reload()

    def reload(self) -> None:
        s, e = date_edit_iso(self.start), date_edit_iso(self.end)
        self.gains = portfolio.capital_gains(self.conn, self.account_id, s or None, e or None)
        _fill(self.table, [[
            g.symbol, fmt_date(g.acquired) if g.acquired else "(unknown)", fmt_date(g.sold),
            _item(str(g.quantity), Qt.AlignRight), _money(g.proceeds), _money(g.basis),
            _money(g.gain), TERM_LABELS.get(g.term, g.term)] for g in self.gains])
        self.summary = portfolio.gains_summary(self.gains)
        parts = []
        for key, label in (("short", "Short-term"), ("long", "Long-term"),
                           ("unknown", "Unknown term")):
            v = self.summary[key]
            if v["lots"]:
                parts.append(f"{label} {fmt_money(v['gain'])} ({v['lots']} lot"
                             f"{'s' if v['lots'] != 1 else ''})")
        t = self.summary["total"]
        parts.append(f"Total proceeds {fmt_money(t['proceeds'])}, basis {fmt_money(t['basis'])}, "
                     f"gain/loss {fmt_money(t['gain'])}")
        self.footer.setText("    ".join(parts))


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------
class PerformanceDialog(QDialog):
    """The money-weighted return (IRR) of the account, or of one security in
    it, over a period: what it earned on the money it actually held."""

    def __init__(self, conn, account_id, parent=None, start: Optional[str] = None,
                 end: Optional[str] = None):
        super().__init__(parent)
        self.conn, self.account_id = conn, int(account_id)
        acct = ledger.get_account(conn, account_id)
        name = acct["name"] if acct else ""
        self.setWindowTitle(f"Performance - {name}")
        end = end or _default_as_of(conn)
        start = start or f"{end[:4]}-01-01"
        self.symbol = QComboBox()
        self.symbol.addItem("Whole account", None)
        for s in _symbols(conn, account_id):
            self.symbol.addItem(s, s)
        self.start = make_date_edit(self, start)
        self.end = make_date_edit(self, end)
        self.labels = {k: QLabel() for k in
                       ("start_value", "money_in", "money_out", "income", "end_value",
                        "gain", "irr")}
        form = QFormLayout()
        form.addRow("Security", self.symbol)
        form.addRow("From", self.start)
        form.addRow("To", self.end)
        form.addRow("Starting value", self.labels["start_value"])
        form.addRow("Money in", self.labels["money_in"])
        form.addRow("Money out", self.labels["money_out"])
        form.addRow("Income received", self.labels["income"])
        form.addRow("Ending value", self.labels["end_value"])
        form.addRow("Gain", self.labels["gain"])
        form.addRow("Return (IRR)", self.labels["irr"])
        note = QLabel("Money-weighted: the annualized rate at which the starting value, "
                      "the money moved in and out on their dates, and the ending value "
                      "net to zero. Buys, sells and dividends inside the account move "
                      "value around, not across the boundary; for one security they are "
                      "the boundary.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)
        self.symbol.currentIndexChanged.connect(lambda *_: self.reload())
        self.start.dateChanged.connect(lambda *_: self.reload())
        self.end.dateChanged.connect(lambda *_: self.reload())
        self.reload()

    def reload(self) -> None:
        s, e = date_edit_iso(self.start), date_edit_iso(self.end)
        if not s or not e or s > e:
            for lab in self.labels.values():
                lab.setText("")
            self.labels["irr"].setText("(the period ends before it starts)")
            self.performance = None
            return
        sym = self.symbol.currentData()
        p = (portfolio.security_performance(self.conn, self.account_id, sym, s, e) if sym
             else portfolio.account_performance(self.conn, self.account_id, s, e))
        self.performance = p
        self.labels["start_value"].setText(fmt_money(p.start_value))
        self.labels["money_in"].setText(fmt_money(p.money_in))
        self.labels["money_out"].setText(fmt_money(p.money_out))
        self.labels["income"].setText(fmt_money(p.income))
        self.labels["end_value"].setText(fmt_money(p.end_value))
        self.labels["gain"].setText(fmt_money(p.gain))
        self.labels["irr"].setText(
            f"{p.irr * 100:.2f}% per year" if p.irr is not None
            else "n/a (nothing was held, or the flows do not settle on a rate)")


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------
class AllocationDialog(QDialog):
    """Where the money is, as of a date: by asset class, by security and by
    account, with a pie of whichever of those three the user is looking at.

    Two things here go past Quicken deliberately. **Scope**: Quicken allocates
    investment accounts and nothing else, so a house is simply absent from the
    answer (its own forums tell you to fake one with a dummy security); this
    window offers investments, investments plus cash accounts, or everything
    owned, and the choice is remembered. **Classification in place**: a
    security's asset class is set right on the By security tab and an account's
    own class -- what a property or a savings balance counts as -- on the By
    account tab, rather than in a separate list. Debt is in no scope: an
    allocation is of what you own.

    The pie groups everything under 10% into one **Other** wedge and drops the
    label off anything still under 5% (:class:`~mammon.ui.charts.SlicesPieCanvas`):
    a dozen 2% slices otherwise stack their labels on one arc and the chart
    stops saying anything. Clicking Other breaks it out at full size; the Back
    button (or a click off the pie) returns.
    """
    CLASS_HEADERS = ["Asset class", "Value", "%"]
    SEC_HEADERS = ["Security", "Asset class", "Mixture", "Value", "%"]
    ACCT_HEADERS = ["Account", "Asset class", "Value", "%"]
    BY_SECURITY_NOTE = "(by security)"

    def __init__(self, conn, parent=None, account_ids=None, as_of: Optional[str] = None,
                 scope: Optional[str] = None):
        super().__init__(parent)
        self.conn = conn
        self.account_ids = None if account_ids is None else [int(a) for a in account_ids]
        self.as_of = as_of or _default_as_of(conn)
        # A caller that named the accounts (the register's gear) has already
        # decided the scope; the picker is for the free-standing window.
        self.scope = scope or prefs.allocation_scope()
        self.setWindowTitle("Asset Allocation")
        self.resize(760, 620)
        self.tabs = QTabWidget()
        self.class_table = _table(self.CLASS_HEADERS)
        self.sec_table = _table(self.SEC_HEADERS)
        self.acct_table = _table(self.ACCT_HEADERS)
        self.tabs.addTab(self.class_table, "By asset class")
        self.tabs.addTab(self.sec_table, "By security")
        self.tabs.addTab(self.acct_table, "By account")
        # The pie follows the tab: three groupings, one chart, so the picture
        # always answers the question the table in front of it is answering.
        self.tabs.currentChanged.connect(lambda *_: self._draw_chart())
        self.chart_box = QVBoxLayout()
        # Clicking the pie's Other wedge breaks it out; this is the way back.
        # A drill-down without one is a trap, so the button ships with it --
        # clicking off the pie works too, but nothing on screen would say so.
        self.back_btn = QPushButton("← Back to all slices")
        self.back_btn.setAutoDefault(False)
        self.back_btn.setVisible(False)
        self.back_btn.clicked.connect(self._zoom_chart_out)
        back_row = QHBoxLayout()
        back_row.addStretch(1)
        back_row.addWidget(self.back_btn)
        self.footer = QLabel()
        self.footer.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        self.scope_combo = QComboBox()
        for key in portfolio.ALLOCATION_SCOPES:
            self.scope_combo.addItem(portfolio.ALLOCATION_SCOPE_LABELS[key], key)
        i = self.scope_combo.findData(self.scope)
        self.scope_combo.setCurrentIndex(i if i >= 0 else 0)
        self.scope_combo.currentIndexChanged.connect(lambda *_: self._set_scope())
        # One security is not always one class: a target-date fund is ~58%
        # equity / 40% bonds / 2% cash, and counting it as one makes every other
        # class read wrong by the difference.
        self.mixtures_btn = QPushButton("Get fund mixtures…")
        self.mixtures_btn.setAutoDefault(False)
        self.mixtures_btn.setToolTip(
            "Look up what each fund actually holds (stocks / bonds / cash) and "
            "split its value across those classes instead of counting the whole "
            "position as one.")
        self.mixtures_btn.clicked.connect(self.fetch_mixtures)
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Show accounts:"))
        scope_row.addWidget(self.scope_combo, 1)
        scope_row.addWidget(self.mixtures_btn)
        self.scope_row = QWidget()
        self.scope_row.setLayout(scope_row)
        self.scope_row.setVisible(self.account_ids is None)

        # A sweep is a fund you own shares of AND the account's spendable
        # balance; which one the picture should show is the user's call, so it
        # is a toggle rather than a rule. Off by default -- see
        # prefs.money_market_as_cash -- and it never changes the total.
        self.mm_check = QCheckBox("Count money-market funds as cash")
        self.mm_check.setToolTip(
            "Show money-market (sweep) holdings under Cash instead of their own "
            "asset class. The total is the same either way.")
        self.mm_check.setChecked(prefs.money_market_as_cash())
        self.mm_check.stateChanged.connect(lambda *_: self._set_money_market_as_cash())
        mm_row = QHBoxLayout()
        mm_row.addWidget(self.mm_check)
        mm_row.addStretch(1)
        self.mm_row = QWidget()
        self.mm_row.setLayout(mm_row)

        note = QLabel(f"Valued as of {fmt_date(self.as_of)}. Set a security's asset class "
                      "on the By security tab and an account's own class -- what a house "
                      "or a savings balance counts as -- on the By account tab; nothing "
                      "is guessed. Debts are not shown: an allocation is of what you own.")
        note.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.scope_row)
        layout.addWidget(self.mm_row)
        layout.addWidget(self.tabs)
        layout.addLayout(self.chart_box)
        layout.addLayout(back_row)
        layout.addWidget(self.footer)
        layout.addWidget(buttons)
        self._chart = None
        self.reload()

    # -- scope -------------------------------------------------------------
    def _set_scope(self) -> None:
        """Show accounts: remembered, so the window opens on the same scope."""
        self.scope = self.scope_combo.currentData() or portfolio.ALLOCATION_SCOPES[0]
        prefs.set_allocation_scope(self.scope)
        self.reload()

    def _set_money_market_as_cash(self) -> None:
        """Sweep-as-cash: remembered, like the scope, so the window opens the
        way the user last read it."""
        prefs.set_money_market_as_cash(self.mm_check.isChecked())
        self.reload()

    def _allocate(self):
        """The ONE place this window asks portfolio for its numbers, so every
        refresh honors the same choices (scope, sweep-as-cash) -- three call
        sites drifting apart is how one tab ends up disagreeing with another."""
        return portfolio.allocation(self.conn, self.account_ids, self.as_of,
                                    scope=self.scope,
                                    money_market_as_cash=prefs.money_market_as_cash())

    # -- drawing -----------------------------------------------------------
    def _class_combo(self, current) -> QComboBox:
        combo = QComboBox()
        combo.addItem("Unclassified", None)
        for cls in portfolio.ASSET_CLASSES:
            combo.addItem(portfolio.ASSET_CLASS_LABELS[cls], cls)
        i = combo.findData(current)
        combo.setCurrentIndex(i if i >= 0 else 0)
        return combo

    def reload(self) -> None:
        a = self._allocate()
        self.allocation = a
        self._fill_classes()
        classes = {r["symbol"]: r["asset_class"] for r in portfolio.list_securities(self.conn)}
        mixtures = security_mix.all_mixtures(self.conn)
        self.sec_table.setRowCount(len(a.by_security))
        for r, sec in enumerate(a.by_security):
            self.sec_table.setItem(r, 0, _item(sec.key))
            combo = self._class_combo(classes.get(sec.key))
            mix = mixtures.get(sec.key)
            if mix:
                # With a mixture the single class no longer decides where the
                # value goes -- it only says WHICH equity bucket the fund's stock
                # slice lands in, since no provider publishes the domestic /
                # international split. Saying so beats a combo that looks
                # authoritative and is not.
                combo.setToolTip(
                    "This fund's value is split by its mixture. The class here "
                    "only chooses which equity bucket its stock portion counts "
                    "in -- refetch the mixture after changing it.")
            combo.currentIndexChanged.connect(
                lambda _i, sym=sec.key, c=combo: self._classify(sym, c.currentData()))
            self.sec_table.setCellWidget(r, 1, combo)
            meta = security_mix.mixture_meta(self.conn, sec.key) if mix else None
            cell = _item(security_mix.describe(mix) if mix else "")
            if meta and meta.get("as_of"):
                cell.setToolTip(f"{meta.get('source') or 'manual'}, "
                                f"as of {fmt_date(meta['as_of'])}")
            self.sec_table.setItem(r, 2, cell)
            self.sec_table.setItem(r, 3, _money(sec.value))
            self.sec_table.setItem(r, 4, _item(f"{sec.pct:.1f}", Qt.AlignRight))
        self.acct_table.setRowCount(len(a.by_account))
        for r, acc in enumerate(a.by_account):
            aid = int(acc.key)
            self.acct_table.setItem(r, 0, _item(acc.label))
            if aid in a.account_classes:
                # A whole-balance account (property, savings): its class is a
                # property OF THE ACCOUNT, so it is set here. An explicit class
                # is shown as chosen; an unsaid one shows the default the
                # allocation used ("Cash" for a cash-shaped account).
                row = ledger.get_account(self.conn, aid)
                saved = (row["asset_class"] if row is not None else None) or None
                combo = self._class_combo(saved or a.account_classes[aid])
                combo.currentIndexChanged.connect(
                    lambda _i, _aid=aid, c=combo: self._classify_account(_aid, c.currentData()))
                self.acct_table.setCellWidget(r, 1, combo)
            else:
                self.acct_table.removeCellWidget(r, 1)
                self.acct_table.setItem(r, 1, _item(self.BY_SECURITY_NOTE))
            self.acct_table.setItem(r, 2, _money(acc.value))
            self.acct_table.setItem(r, 3, _item(f"{acc.pct:.1f}", Qt.AlignRight))
        text = f"Total {fmt_money(a.total)}"
        if a.unpriced:
            text += f"    Unpriced, left out: {', '.join(a.unpriced)}"
        if a.cash_only_accounts:
            # A balance with no holdings recorded reads as a cash allocation and
            # is indistinguishable from a real one. Naming the accounts is the
            # difference between "why is my cash so high" and a fixable answer.
            names = ", ".join(f"{n} {fmt_money(c)}" for n, c in a.cash_only_accounts)
            total = sum(c for _, c in a.cash_only_accounts)
            text += (f"\nNote: {fmt_money(total)} of the Cash above is the balance of "
                     f"investment accounts with NO holdings recorded, so the ledger "
                     f"can only count it as cash: {names}. Enter their holdings, or "
                     f"hide the account to leave it out of totals.")
        self.footer.setText(text)
        self._draw_chart()

    def _fill_classes(self) -> None:
        _fill(self.class_table,
              [[s.label, _money(s.value), _item(f"{s.pct:.1f}", Qt.AlignRight)]
               for s in self.allocation.by_class])

    def fetch_mixtures(self) -> None:
        """Look up each held security's own composition and store it.

        Reports what it could not place rather than guessing: a fund whose
        equity bucket is unknown comes back with that slice UNCLASSIFIED and is
        named here, with the provider's category as a suggestion, because the
        domestic/international split is the one thing the data cannot say."""
        symbols = [s.key for s in self.allocation.by_security]
        if not symbols:
            self._warn("Fund mixtures", "No priced holdings to look up.")
            return
        try:
            report = security_mix.fetch_mixtures(self.conn, symbols,
                                                 source=self._mixture_source())
        except Exception as exc:
            self._warn("Fund mixtures", str(exc))
            return
        self.reload()
        lines = [f"{len(report.written)} fund(s) split across their asset classes."]
        if report.needs_stock_class:
            names = ", ".join(
                f"{sym} (looks like {portfolio.ASSET_CLASS_LABELS.get(hint, hint)})"
                if hint else sym
                for sym, hint in report.needs_stock_class)
            lines.append(
                f"\nThese hold stock but have no equity class set, so that part "
                f"is Unclassified: {names}.\nSet each one's asset class on this "
                f"tab, then run this again.")
        if report.missing:
            lines.append("\nNo composition published for: "
                         + ", ".join(sym for sym, _ in report.missing) + ".")
        self._warn("Fund mixtures", "".join(lines))

    def _mixture_source(self):
        """The composition backend (a seam tests replace; None = the default)."""
        return None

    def _warn(self, title: str, text: str) -> None:
        """Overridable so the window stays testable headless."""
        QMessageBox.information(self, title, text)

    def _classify(self, symbol: str, asset_class) -> None:
        """A change in the By security tab's class column is saved at once and
        the other tabs re-summed."""
        portfolio.set_security(self.conn, symbol, asset_class=asset_class or "")
        self.allocation = self._allocate()
        self._fill_classes()
        self._draw_chart()

    def _classify_account(self, account_id: int, asset_class) -> None:
        """The By account tab's class column for a whole-balance account (a
        house, a savings balance). Saved at once, like a security's."""
        portfolio.set_account_asset_class(self.conn, account_id, asset_class)
        self.allocation = self._allocate()
        self._fill_classes()
        self._draw_chart()

    def chart_slices(self) -> list:
        """The (label, cents) pairs the pie draws: whichever grouping the user
        is looking at."""
        a = self.allocation
        rows = (a.by_security if self.tabs.currentIndex() == 1 else
                a.by_account if self.tabs.currentIndex() == 2 else a.by_class)
        return [(s.label, s.value) for s in rows if s.value > 0]

    def _on_chart_zoom(self, path) -> None:
        self.back_btn.setVisible(bool(path))

    def _zoom_chart_out(self) -> None:
        if self._chart is not None:
            self._chart.zoom_out()

    def _draw_chart(self) -> None:
        """A pie of the current tab's grouping, when matplotlib is around;
        silently none otherwise (the tables carry the numbers)."""
        try:
            from mammon.ui.charts import SlicesPieCanvas
        except Exception:                               # matplotlib missing
            return
        if self._chart is not None:
            self.chart_box.removeWidget(self._chart)
            self._chart.setParent(None)
            self._chart.deleteLater()
        self._chart = SlicesPieCanvas(self.tabs.tabText(self.tabs.currentIndex()),
                                      self.chart_slices(), parent=self)
        self._chart.setMinimumHeight(240)
        self.chart_box.addWidget(self._chart)
        # A fresh canvas is always at the top level, so the button starts hidden
        # and follows the canvas from there (it drills on a click of its own).
        self._chart.zoomChanged.connect(self._on_chart_zoom)
        self._on_chart_zoom(self._chart.zoom_path())


# ---------------------------------------------------------------------------
# Specify Lots (on a sale row)
# ---------------------------------------------------------------------------
class SpecifyLotsDialog(QDialog):
    """Name the lots a sale disposes of (Quicken's Specify Lots). Lists the
    lots open the day before the sale with an Assign column; the named shares
    are relieved first, the rest by the account's method. Saved through
    ``investments.assign_lots``, which validates."""
    HEADERS = ["Acquired", "Shares held", "Cost", "Cost / share", "Assign"]

    def __init__(self, conn, sale_txn_id: int, parent=None):
        super().__init__(parent)
        self.conn, self.sale_id = conn, int(sale_txn_id)
        self.sale = investments.get_investment_txn(conn, self.sale_id)
        if self.sale is None:
            raise KeyError(f"no investment transaction {sale_txn_id}")
        sym = self.sale["symbol"] or ""
        self.setWindowTitle(f"Specify Lots - {sym} sold {fmt_date(self.sale['date'])}")
        self.resize(640, 380)
        before = (_dt.date.fromisoformat(self.sale["date"]) - _dt.timedelta(days=1)).isoformat()
        self.lots = [l for l in portfolio.open_lots(conn, self.sale["account_id"], symbol=sym,
                                                    as_of=before) if l.txn_id is not None]
        current = dict(investments.lot_assignments_for(conn, self.sale_id))
        self.sale_qty = investments._D(self.sale["quantity"])
        note = QLabel(f"This sale disposes of {self.sale_qty} shares. Enter how many come "
                      "from each lot; shares not assigned are taken by the account's "
                      "cost-basis method. Lots bought on the day of the sale are not "
                      "listed.")
        note.setWordWrap(True)
        self.table = _table(self.HEADERS)
        self.table.setRowCount(len(self.lots))
        self.edits = []
        for r, l in enumerate(self.lots):
            self.table.setItem(r, 0, _item(fmt_date(l.acquired)))
            self.table.setItem(r, 1, _item(str(l.quantity), Qt.AlignRight))
            self.table.setItem(r, 2, _money(l.cost))
            per = (investments._D(l.cost) / l.quantity / 100) if l.quantity else None
            self.table.setItem(r, 3, _item("" if per is None else f"{per:.4f}", Qt.AlignRight))
            edit = QLineEdit(current.get(l.txn_id, ""))
            edit.setPlaceholderText("0")
            edit.textChanged.connect(lambda *_: self._sync_footer())
            self.table.setCellWidget(r, 4, edit)
            self.edits.append(edit)
        self.footer = QLabel()
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.table)
        layout.addWidget(self.footer)
        layout.addWidget(buttons)
        self._sync_footer()

    def assignments(self) -> list:
        out = []
        for l, edit in zip(self.lots, self.edits):
            text = edit.text().strip()
            if not text:
                continue
            out.append((l.txn_id, text))
        return out

    def _sync_footer(self) -> None:
        total = investments._D("0")
        for _lot, q in self.assignments():
            try:
                total += investments._D(q)
            except Exception:
                pass
        self.footer.setText(f"Assigned {total} of {self.sale_qty} shares.")

    def _warn(self, msg: str) -> None:
        QMessageBox.warning(self, "Specify Lots", msg)

    def accept(self) -> None:
        try:
            investments.assign_lots(self.conn, self.sale_id, self.assignments())
        except (ValueError, KeyError) as exc:
            self._warn(str(exc))
            return
        super().accept()

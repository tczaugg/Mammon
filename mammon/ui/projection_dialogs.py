"""Projected Balances (a dialog) and the Financial Calendar (roadmap item 5).

Both are views over :func:`mammon.projection.project`: one balance per day
across the chosen accounts from today forward, built from entered rows, the
reminders not yet entered, loan schedules and -- the part Quicken never had --
predictions read off recent history (:mod:`mammon.predictions`). Projected
Balances shows the horizon as an event table with a running balance and a
chart; the Calendar lays one month out as a grid, each day carrying its events
and its end-of-day balance (actual for days already past, projected ahead).

The Calendar is a :class:`CalendarPanel` -- a PAGE OF THE REGISTER AREA rather
than a window, shown there at startup and again from Tools > Financial
Calendar. See the class docstring for why it is not a modal.

The calendar colors each event by what it is, in both themes: a scheduled
payment red, a scheduled deposit green, a predicted payment yellow, a
predicted deposit blue, a pending pre-entry muted, an entered row plain. A
right-click on a day offers, for each prediction on it, Dismiss (the estimate
was wrong) and Schedule (the user knows the real amount and date, and a
definition supersedes the guess); dismissed predictions can be restored.

Projected Balances has an account picker: the SPENDING accounts -- checking,
savings, credit and cash -- and "All spending accounts" summed. The calendar
instead has a progressive row of account SLOTS across its top
(:class:`AccountSlots`): it starts as a single "+ account filter", and each
account chosen reveals one more empty "+" slot (never a row of empties), up to
ten. Click or right-click a slot to choose its account, the × beside it to
clear it; the month is summed over the filled slots, or over every spending
account when none is filled. The choice is a display preference (QSettings),
so the calendar opens on the same accounts next time. Assets and loans are not
spending accounts: summing the house and the mortgage into "all spending
accounts" once put the projection over a million dollars against a checking
balance of a hundred thousand.

The calendar has a second MODE. "Budget burn-down" swaps the day figure from a
projected balance to what is LEFT of the month's budget: the month opens at the
allowance (every budgeted category's target plus its rollover) and each day
subtracts that day's expenses -- entered so far, scheduled or predicted after --
while income is ignored entirely. A month can therefore burn down to nothing
while the accounts are perfectly healthy, and vice versa, which is why it is a
toggle beside the balance view and never a replacement for it. The model is per
category underneath even though the day figure is the household total, so an
expense that takes its own category past its limit -- and every later expense in
that blown category -- carries a moneybag mark whose tooltip names the category.
The account slots still narrow the SPEND side here, while the allowance stays the
whole household's, so "left" in this mode means "left after these accounts".
Budget mode also takes over the band under the calendar: the trend chart gives
way to one bar per budget item, green for what is left of it and red outside the
bar's left edge for an overrun (:mod:`mammon.ui.budget_bars`), because envelope
trouble is per item and a household total cannot say which item. See
:mod:`mammon.budgets` (``burn_down``, ``month_category_status``) for the
arithmetic.

Neither of them writes anything except a prediction dismissal or, through the
scheduled-payment editor, a new definition.
"""
from __future__ import annotations

import datetime as _dt
import html
from typing import Optional

from PyQt5.QtCore import QDate, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu, QPushButton,
    QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from mammon import budgets, ledger, predictions, projection, scheduled
from mammon.ui import prefs, style
from mammon.ui.models import fmt_cents, fmt_date
from mammon.ui.delegates import NoWheelComboBox

ALL_SPENDING = "All spending accounts"
SPENDING_TYPES = predictions.SPENDING_TYPES

# Event colors by theme: (light, dark).
_COLORS = {
    "scheduled_out": ("#b2382c", "#ff6b6b"),     # red
    "scheduled_in": ("#2e6b4e", "#7dbb98"),      # green
    "predicted_out": ("#a8701a", "#e5c07b"),     # yellow (amber on white)
    "predicted_in": ("#1f5fa8", "#6fb1ff"),      # blue
}
# Today's highlight fill: (light, dark). Light is a whisper-of-blue near-white --
# only a shade darker than the page -- so today reads as a gentle highlight, not
# a heavy block; the earlier light green (#e4efe8) sat too dark under the day's
# text (reported). Dark keeps its deeper fill so the highlight is visible against
# the dark grid.
_TODAY_BG = ("#eef4fc", "#2a3b31")

# The over-budget mark: (light, dark). ORANGE-RED ink, at the user's direction
# ("The moneybag symbol should be orange-red"); it was neutral gray before and
# read as furniture. The meaning still rides on the SHAPE, not the hue -- a
# moneybag pictograph, which is why the mark is a generated PNG rather than a
# character (no font off Windows can be relied on for one). Orange-red is
# deliberately NOT the amber above: amber means missing or conflicting data
# everywhere else in Mammon, and this is a definite fact about definite numbers.
# One hue for both themes, the dark entry lifted so it carries against the dark
# grid.
_MARK_COLOR = ("#d93d0b", "#ff6a3d")
#: Mark edge in pixels. A day cell's lines are small; 13px sits on the text
#: baseline without stretching the row, and the bag still reads at that size.
MARK_SIZE = 13
#: What a cell shows instead of the mark when the icon cache cannot be written.
MARK_FALLBACK = "[over]"


def _today() -> str:
    return QDate.currentDate().toString("yyyy-MM-dd")


def _dark() -> bool:
    return style.theme() == "dark"


def event_color(e) -> Optional[str]:
    """The color an event is drawn in, for the active theme; None for an
    entered row (plain text)."""
    i = 1 if _dark() else 0
    if e.source == projection.PREDICTED:
        return _COLORS["predicted_out" if e.amount < 0 else "predicted_in"][i]
    if e.source in (projection.SCHEDULED, projection.LOAN):
        return _COLORS["scheduled_out" if e.amount < 0 else "scheduled_in"][i]
    if e.pending:
        return style.muted_color()
    return None


def today_background() -> str:
    return _TODAY_BG[1 if _dark() else 0]


def event_mark(e) -> str:
    """The prefix that says what kind of line this is in plain text: nothing
    for an entered row, · for a reminder not yet entered, ~ for an estimate."""
    if e.source == projection.PREDICTED:
        return "~ "
    if e.source == projection.ENTERED and not e.pending:
        return ""
    return "· "


def mark_html(size: int = MARK_SIZE) -> str:
    """The over-budget mark as inline rich text.

    Rich text resolves ``src`` through QFile, so the mark has to be a file on
    disk; :mod:`mammon.ui.branch_icons` generates one per (shape, color, size)
    and hands back its path. When the cache cannot be written (a read-only
    install) it returns None and the cell falls back to a text token rather
    than showing a broken-image box.
    """
    from mammon.ui import branch_icons

    p = branch_icons.icon_path("moneybag", _MARK_COLOR[1 if _dark() else 0],
                               size, prefix="mark")
    if p is None:
        return html.escape(MARK_FALLBACK)
    return '<img src="%s" width="%d" height="%d">' % (p.as_uri(), size, size)


def source_label(e) -> str:
    if e.source == projection.PREDICTED:
        return "Predicted (automatic)" if e.automatic else "Predicted from history"
    return {"entered": "Entered (pending)" if e.pending else "Entered",
            "scheduled": "Scheduled", "loan": "Loan schedule"}[e.source]


def spending_accounts(conn) -> list:
    """The accounts a projection is about: visible checking, savings, credit
    and cash. Not investments (not a cash question), not assets or loans
    (their balances are worth and debt, not money to spend)."""
    return [a for a in ledger.list_accounts(conn, include_closed=False, include_hidden=False)
            if (a["type"] or "") in SPENDING_TYPES]


def _account_combo(conn, account_id=None) -> QComboBox:
    combo = NoWheelComboBox()
    combo.addItem(ALL_SPENDING, None)
    for a in spending_accounts(conn):
        combo.addItem(a["name"], int(a["id"]))
    if account_id is not None:
        i = combo.findData(int(account_id))
        if i >= 0:
            combo.setCurrentIndex(i)
    return combo


def _account_ids(conn, combo) -> list[int]:
    aid = combo.currentData()
    if aid is not None:
        return [int(aid)]
    return [int(a["id"]) for a in spending_accounts(conn)]


def _money_item(cents: int) -> QTableWidgetItem:
    item = QTableWidgetItem(fmt_cents(cents))
    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    if cents < 0:
        item.setForeground(QColor(style.negative_color()))
    return item


class _SlotLabel(QLabel):
    """A label that reports a left click with its global position, so a slot
    opens its picker on either button."""
    clicked = pyqtSignal(object)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.mapToGlobal(event.pos()))
        super().mousePressEvent(event)


class AccountSlots(QWidget):
    """A progressive row of account slots: the accounts a calendar month is
    summed over. It shows a single "+ account filter" to start; each account
    chosen reveals exactly one more empty "+" slot (only the next empty is ever
    shown, never a trailing row of them), up to ``MAX`` slots. Click or
    right-click a slot to choose its account from the spending accounts (or
    clear it); the × beside a filled slot clears it, and clearing compacts the
    remaining choices so no gap is left behind. No filled slot means every
    spending account. The choice is kept in QSettings
    (``prefs.projection_slots``) unless ``persist`` is off."""
    MAX = 10
    EMPTY_FIRST = "+ account filter"     # slot 1, when empty
    EMPTY_MORE = "+"                     # the revealed next empty slot
    changed = pyqtSignal()

    def __init__(self, conn, parent=None, ids=None, persist: bool = True):
        super().__init__(parent)
        self.conn = conn
        self.persist = persist
        self._ids: list = [None] * self.MAX
        self._boxes: list = []
        self._labels: list = []
        self._clears: list = []
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        for i in range(self.MAX):
            box = QFrame()
            box.setObjectName("accountSlot")
            box.setFrameShape(QFrame.StyledPanel)
            row = QHBoxLayout(box)
            row.setContentsMargins(6, 1, 1, 1)
            row.setSpacing(2)
            lab = _SlotLabel()
            lab.setCursor(Qt.PointingHandCursor)
            lab.setToolTip("Click to choose the account for this slot.")
            lab.setContextMenuPolicy(Qt.CustomContextMenu)
            lab.customContextMenuRequested.connect(
                lambda pos, i=i, lab=lab: self.pick(i, lab.mapToGlobal(pos)))
            lab.clicked.connect(lambda gpos, i=i: self.pick(i, gpos))
            clear = QToolButton()
            clear.setText("×")
            clear.setAutoRaise(True)
            clear.setToolTip("Clear this slot")
            clear.clicked.connect(lambda _c=False, i=i: self.clear_slot(i))
            row.addWidget(lab)
            row.addWidget(clear)
            lay.addWidget(box)
            self._boxes.append(box)
            self._labels.append(lab)
            self._clears.append(clear)
        lay.addStretch(1)
        initial = ids if ids is not None else (prefs.projection_slots() if persist else [])
        self.set_account_ids(initial, save=False)

    # -- state ---------------------------------------------------------------
    def available(self) -> list:
        return [(int(a["id"]), a["name"]) for a in spending_accounts(self.conn)]

    def account_ids(self) -> list:
        return [i for i in self._ids if i is not None]

    def set_account_ids(self, ids, save: bool = True) -> None:
        known = {aid for aid, _ in self.available()}
        clean: list = []
        for i in ids or []:
            if i is None:
                continue
            i = int(i)
            if i in known and i not in clean:
                clean.append(i)
        clean = clean[:self.MAX]
        self._ids = clean + [None] * (self.MAX - len(clean))
        self._after_change(save)

    def set_slot(self, slot: int, account_id, save: bool = True) -> None:
        aid = None if account_id is None else int(account_id)
        if aid is not None:
            if aid not in {a for a, _ in self.available()}:
                return
            for j, cur in enumerate(self._ids):          # one account, one slot
                if cur == aid and j != slot:
                    self._ids[j] = None
        self._ids[slot] = aid
        self._after_change(save)

    def clear_slot(self, slot: int) -> None:
        self.set_slot(slot, None)

    def _after_change(self, save: bool) -> None:
        # Compact: the chosen accounts sit contiguously at the front, so a
        # cleared middle slot leaves no gap and the "next empty" is always last.
        filled = [i for i in self._ids if i is not None]
        self._ids = filled + [None] * (self.MAX - len(filled))
        names = dict(self.available())
        visible = min(len(filled) + 1, self.MAX)   # filled slots plus one empty
        for i, lab in enumerate(self._labels):
            aid = self._ids[i]
            name = names.get(aid) if aid is not None else None
            lab.setText(name if name else
                        (self.EMPTY_FIRST if i == 0 else self.EMPTY_MORE))
            f = QFont(lab.font())
            f.setBold(name is not None)
            lab.setFont(f)
            self._clears[i].setVisible(name is not None)
            self._boxes[i].setVisible(i < visible)
        if save and self.persist:
            prefs.set_projection_slots(self.account_ids())
        self.changed.emit()

    def describe(self) -> str:
        names = dict(self.available())
        chosen = [names[i] for i in self.account_ids() if i in names]
        return ", ".join(chosen) if chosen else ALL_SPENDING

    # -- the picker ----------------------------------------------------------
    def pick(self, slot: int, global_pos) -> None:
        menu = QMenu(self)
        current = self._ids[slot]
        choices = []
        for aid, name in self.available():
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(aid == current)
            choices.append((act, aid))
        clear = None
        if current is not None:
            menu.addSeparator()
            clear = menu.addAction("Clear slot")
        chosen = menu.exec_(global_pos)
        if chosen is None:
            return
        if clear is not None and chosen is clear:
            self.clear_slot(slot)
            return
        for act, aid in choices:
            if chosen is act:
                self.set_slot(slot, aid)
                return


def _predictions_box() -> QCheckBox:
    box = QCheckBox("Include predictions from history")
    box.setChecked(True)
    box.setToolTip("Payees paid or received at a steady interval and amount over the "
                   "last few months, projected forward as estimates.")
    return box


class ProjectedBalancesDialog(QDialog):
    """Quicken's Projected Balances: one account or all spending accounts,
    over 7 days to 12 months, as an event table with the running balance and
    a chart with the low point marked."""

    HORIZONS = (("7 days", 7), ("14 days", 14), ("30 days", 30),
                ("90 days", 90), ("12 months", 365))
    DATE, PAYEE, ACCOUNT, AMOUNT, BALANCE, SOURCE = range(6)

    def __init__(self, conn, parent=None, account_id=None, today=None):
        super().__init__(parent)
        self.conn = conn
        self.today = today or _today()
        self.setWindowTitle("Projected Balances")
        self.resize(820, 620)
        self.projection = None

        top = QHBoxLayout()
        top.addWidget(QLabel("Account"))
        self.account = _account_combo(conn, account_id)
        top.addWidget(self.account, 1)
        top.addWidget(QLabel("Next"))
        self.horizon = NoWheelComboBox()
        for label, days in self.HORIZONS:
            self.horizon.addItem(label, days)
        self.horizon.setCurrentIndex(2)
        top.addWidget(self.horizon)
        self.include_predictions = _predictions_box()
        top.addWidget(self.include_predictions)

        self.summary = QLabel("")
        self.summary.setObjectName("registerSub")

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Date", "Payee", "Account", "Amount", "Balance", "Source"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)

        self.chart_box = QVBoxLayout()
        self.chart = None

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.summary)
        lay.addWidget(self.table, 1)
        lay.addLayout(self.chart_box)
        lay.addWidget(buttons)

        self.account.currentIndexChanged.connect(lambda *_: self.refresh())
        self.horizon.currentIndexChanged.connect(lambda *_: self.refresh())
        self.include_predictions.toggled.connect(lambda *_: self.refresh())
        self.refresh()

    def end_date(self) -> str:
        days = int(self.horizon.currentData() or 30)
        return (_dt.date.fromisoformat(self.today) + _dt.timedelta(days=days)).isoformat()

    def refresh(self) -> None:
        ids = _account_ids(self.conn, self.account)
        self.projection = projection.project(
            self.conn, ids, self.today, self.end_date(),
            include_predictions=self.include_predictions.isChecked(), today=self.today)
        p = self.projection
        names = {int(a["id"]): a["name"]
                 for a in ledger.list_accounts(self.conn, include_closed=True,
                                               include_hidden=True)}
        # One row per event with the balance AFTER it (events are applied in
        # order within a day, so the last row of a day equals that day's close).
        rows = []
        running = p.opening
        for day in p.days:
            for e in day.events:
                running += e.amount
                rows.append((e, running))
        self.table.setRowCount(len(rows))
        for i, (e, bal) in enumerate(rows):
            color = event_color(e)
            cells = [QTableWidgetItem(fmt_date(e.date)), QTableWidgetItem(e.payee),
                     QTableWidgetItem(names.get(e.account_id, "")), _money_item(e.amount),
                     _money_item(bal), QTableWidgetItem(source_label(e))]
            if color:
                for col in (self.PAYEE, self.SOURCE):
                    cells[col].setForeground(QColor(color))
            for col, item in enumerate(cells):
                self.table.setItem(i, col, item)
        self.table.resizeColumnsToContents()
        low = f"lowest {fmt_cents(p.low)} on {fmt_date(p.low_date)}"
        self.summary.setText(
            f"Today {fmt_cents(p.opening)}  →  {fmt_date(p.end)} {fmt_cents(p.closing)}"
            f"   ({low})")
        self._rebuild_chart()

    def _rebuild_chart(self) -> None:
        from mammon.ui.charts import ProjectedBalanceCanvas
        if self.chart is not None:
            self.chart_box.removeWidget(self.chart)
            self.chart.setParent(None)
            self.chart.deleteLater()
        self.chart = ProjectedBalanceCanvas(self.projection, self)
        self.chart.setMinimumHeight(220)
        self.chart_box.addWidget(self.chart)


class DismissedPredictionsDialog(QDialog):
    """The predictions the user dismissed, with a way back."""

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Dismissed Predictions")
        self.resize(420, 320)
        self.list = QListWidget()
        self.restore_btn = QPushButton("Restore")
        self.restore_btn.clicked.connect(self.restore_selected)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("These payees are no longer predicted. Restore one to "
                             "see it estimated again."))
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        row.addWidget(self.restore_btn)
        row.addStretch(1)
        row.addWidget(buttons)
        lay.addLayout(row)
        self.reload()

    def reload(self) -> None:
        self.list.clear()
        for d in predictions.dismissed(self.conn):
            item = QListWidgetItem(f"{d['payee']}  ({d['account_name']})")
            item.setData(Qt.UserRole, (d["account_id"], d["payee_key"]))
            self.list.addItem(item)

    def restore_selected(self) -> None:
        for item in self.list.selectedItems():
            aid, key = item.data(Qt.UserRole)
            predictions.restore(self.conn, aid, key)
        self.reload()


class CalendarPanel(QWidget):
    """Quicken's Financial Calendar: a month grid, Sunday first, each day
    listing its events and closing with that day's balance across the chosen
    accounts. Days before today show what was entered; days from today on show
    the projection, predictions included. Prev/Next step the month.

    This is a PAGE OF THE REGISTER AREA, not a modal: the main window shows it
    where a register goes, on startup and from Tools > Financial Calendar. A
    calendar is a thing you glance at while working the register -- a modal
    that must be dismissed before the next edit is the wrong shape for that,
    and it could not stay open across account switches either.

    Because it now outlives the writes going on around it, a write elsewhere
    calls :meth:`mark_stale`: the month is recomputed when the panel is next
    SHOWN, so an edit in a register never pays for a projection nobody is
    looking at.
    """

    WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
    MAX_EVENTS = 4

    def __init__(self, conn, parent=None, year=None, month=None, account_id=None,
                 today=None):
        super().__init__(parent)
        self.conn = conn
        self.today = today or _today()
        # A page that lives as long as the window can be looked at tomorrow:
        # unless a caller pinned the date (tests do), the panel follows the
        # clock, so the highlighted day and the actual/projected split are not
        # yesterday's when the app was left running overnight.
        self._follow_clock = today is None
        t = _dt.date.fromisoformat(self.today)
        self.year = year or t.year
        self.month = month or t.month
        self.projection = None
        self.burn = None
        self._plain: dict = {}
        self._html: dict = {}
        self._tips: dict = {}
        self._events: dict = {}
        self._marks: dict = {}
        self._budget_name = None
        self._budget_id = None
        self._stale = False

        # The title box is a register's: same object names, same insets, so
        # switching between the calendar and a register does not shift the
        # layout under the user.
        self.header_box = QFrame()
        self.header_box.setObjectName("registerTitleBox")
        top = QHBoxLayout(self.header_box)
        top.setContentsMargins(0, 0, 0, 0)
        self.header = QLabel("Financial Calendar")
        self.header.setObjectName("registerTitle")
        top.addWidget(self.header)
        top.addStretch(1)
        self.prev_btn = QPushButton("◀")
        self.prev_btn.setAutoDefault(False)
        self.prev_btn.clicked.connect(self.prev_month)
        self.next_btn = QPushButton("▶")
        self.next_btn.setAutoDefault(False)
        self.next_btn.clicked.connect(self.next_month)
        self.title = QLabel("")
        font = QFont(self.title.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 2)
        self.title.setFont(font)
        # Pin the month title to the width of the widest "%B %Y" of the year.
        # The arrows sit either side of it after a stretch, so a title that
        # sized itself to the current month ("May 2026" vs "September 2026")
        # dragged prev_btn sideways every time the month changed - the user
        # had to look up to find the back arrow instead of clicking the same
        # spot repeatedly. Measured, never hardcoded to September, so a
        # different font, locale or a wider year string stays correct.
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setFixedWidth(self._title_width(font))
        top.addWidget(self.prev_btn)
        top.addWidget(self.title, 0, Qt.AlignCenter)
        top.addWidget(self.next_btn)
        top.addSpacing(16)
        self.include_predictions = _predictions_box()
        top.addWidget(self.include_predictions)
        top.addSpacing(16)
        # The mode toggle. Budget burn-down is an ALTERNATIVE reading of the
        # same month, not a filter on it: it ignores income, so it can show a
        # comfortable month while the balance view shows the account going
        # negative on the 20th. Hence a second view rather than a change to the
        # first one.
        self.budget_mode = QCheckBox("Budget burn-down")
        self.budget_mode.setToolTip(
            "Show what is LEFT of this month's budget each day instead of the\n"
            "projected balance. Income is ignored; savings transfers and the\n"
            "principal part of a loan payment are not spending.")
        top.addWidget(self.budget_mode)
        # How much of the household's recent spending the budget actually
        # covers. A burn-down over a budget that only names two categories is
        # an honest number about the wrong question, and the user cannot tell
        # by looking at the month, so the figure sits on the toggle itself.
        self.coverage = QLabel("")
        self.coverage.setObjectName("registerSub")
        self.coverage.setTextFormat(Qt.RichText)
        top.addWidget(self.coverage)

        # The account slots: which accounts this month is summed over.
        slots_row = QHBoxLayout()
        self.slots_label = QLabel("Accounts")
        slots_row.addWidget(self.slots_label)
        self.slots = AccountSlots(conn, self,
                                  ids=[int(account_id)] if account_id is not None else None)
        slots_row.addWidget(self.slots, 1)

        self.grid = QTableWidget(6, 7)
        self.grid.setHorizontalHeaderLabels(list(self.WEEKDAYS))
        self.grid.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.grid.setSelectionMode(QAbstractItemView.NoSelection)
        self.grid.verticalHeader().setVisible(False)
        self.grid.horizontalHeader().setSectionResizeMode(
            self.grid.horizontalHeader().Stretch)
        self.grid.verticalHeader().setDefaultSectionSize(96)
        self.grid.setWordWrap(True)

        self.summary = QLabel("")
        self.summary.setObjectName("registerSub")
        self.summary.setWordWrap(True)
        self.legend = QLabel("")
        self.legend.setTextFormat(Qt.RichText)
        self.legend.setWordWrap(True)

        # One band under the calendar, whose OCCUPANT FOLLOWS THE MODE. Normally
        # it is a spending-per-month bar chart: the grid answers "what is
        # coming", the chart answers "how has spending trended", the two glances
        # a home page owes. In budget mode the question is "which envelope is in
        # trouble this month", which a twelve-month trend cannot answer, so the
        # band holds a bar per budget item instead (mammon.ui.budget_bars).
        # Either way it lives in its own box so refresh() can swap the throwaway
        # widget without disturbing the calendar.
        self.chart_box = QVBoxLayout()
        self.chart_widget = None

        lay = QVBoxLayout(self)
        # Match the register pages' 8px inset (see RegisterWidget).
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        lay.addWidget(self.header_box)
        lay.addLayout(slots_row)
        lay.addWidget(self.grid, 1)
        lay.addWidget(self.legend)
        lay.addWidget(self.summary)
        lay.addLayout(self.chart_box)
        self.slots.changed.connect(lambda *_: self.refresh())
        self.include_predictions.toggled.connect(lambda *_: self.refresh())
        self.budget_mode.toggled.connect(lambda *_: self.refresh())
        self.refresh()

    # -- staleness -----------------------------------------------------------
    def mark_stale(self) -> None:
        """A write somewhere else changed the balances this month is drawn
        from: recompute now if the panel is on screen, otherwise the next time
        it is shown."""
        self._stale = True
        if self.isVisible():
            self.refresh_if_stale()

    def refresh_if_stale(self) -> bool:
        """Recompute the month iff a write -- or the date rolling over --
        invalidated it since the last draw; returns whether it did."""
        if not self._stale and not (self._follow_clock and self.today != _today()):
            return False
        self.refresh()
        return True

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_if_stale()

    def account_ids(self) -> list:
        """The accounts the month is summed over: the filled slots, else
        every spending account.

        BOTH modes read the same selection. Budget mode once forced every
        spending account here, on the argument that an envelope is household-
        wide; the user overruled it -- a selector that silently means something
        else in one mode is worse than a narrow number, and narrowing to one
        card to ask "what did THIS card do to the plan" is a question worth
        being able to ask. The allowance stays household-wide either way (see
        :func:`mammon.budgets.burn_down`), and :meth:`_summary_text` says so
        whenever the selection is a strict subset.
        """
        return self.slots.account_ids() or self.spending_account_ids()

    def spending_account_ids(self) -> list:
        """Every spending account: what an empty slot selection means."""
        return [int(a["id"]) for a in spending_accounts(self.conn)]

    # -- navigation --------------------------------------------------------
    TITLE_PADDING = 12  # so no month is ever one pixel short of eliding

    @staticmethod
    def _title_width(font: QFont) -> int:
        """Widest rendered '%B %Y' over all twelve months, in `font`.

        Computed rather than hardcoded ("September" is only the longest name
        in this font and locale) and deliberately independent of the current
        date: the year is measured as four copies of the widest digit, so the
        width never changes when the calendar steps into another year.
        """
        fm = QFontMetrics(font)
        widest_digit = max("0123456789", key=fm.width)
        year = widest_digit * 4
        return max(fm.width(_dt.date(2000, m, 1).strftime("%B ") + year)
                   for m in range(1, 13)) + CalendarPanel.TITLE_PADDING

    def prev_month(self) -> None:
        self.year, self.month = (self.year - 1, 12) if self.month == 1 else (self.year, self.month - 1)
        self.refresh()

    def next_month(self) -> None:
        self.year, self.month = (self.year + 1, 1) if self.month == 12 else (self.year, self.month + 1)
        self.refresh()

    # -- what a day holds ---------------------------------------------------
    def cell_text(self, day: int) -> str:
        """The text shown for a day of the current month ('' when blank)."""
        return self._plain.get(int(day), "")

    def cell_html(self, day: int) -> str:
        """The rich text a day's cell was built from ('' when blank)."""
        return self._html.get(int(day), "")

    def cell_tooltip(self, day: int) -> str:
        """The tooltip a day's cell carries ('' when it has none)."""
        return self._tips.get(int(day), "")

    def marks_on(self, day: int) -> list:
        """The over-limit expenses a day is marked for ([] in balance mode)."""
        return list(self._marks.get(int(day), []))

    def events_on(self, day: int) -> list:
        return list(self._events.get(int(day), []))

    def predictions_on(self, day: int) -> list:
        return [e for e in self.events_on(day) if e.source == projection.PREDICTED]

    def _iso(self, day: int) -> str:
        return f"{self.year:04d}-{self.month:02d}-{int(day):02d}"

    # -- drawing --------------------------------------------------------------
    def _split_marks(self, events: list, over) -> tuple:
        """``(one flag per displayed event, marks with no line to sit on)``.

        Every over-limit expense gets its OWN mark, so the two have to be
        matched up rather than counted per day. An expense is matched to a
        displayed event by payee, one for one; what is left over had no line in
        this cell -- the burn-down covers every account and a split contributes
        a mark per leg, while the grid shows at most :attr:`MAX_EVENTS` rows of
        the projection -- and is drawn on the day number instead, so the count
        of marks in a cell always equals the count of offending expenses.
        """
        want: dict = {}
        for x in over:
            want[x.payee.casefold()] = want.get(x.payee.casefold(), 0) + 1
        flags = []
        for e in events:
            key = e.payee.casefold()
            if want.get(key):
                want[key] -= 1
                flags.append(True)
            else:
                flags.append(False)
        return flags, sum(want.values())

    def _cell_html(self, day: int, events: list, balance, *, over=(), left=None) -> str:
        shown = events[:self.MAX_EVENTS]
        flags, extra = self._split_marks(shown, over)
        mark = mark_html() if over else ""
        head = f"<b>{day}</b>" + (("&nbsp;" + mark) * extra)
        lines = [head]
        for e, flagged in zip(shown, flags):
            text = html.escape(f"{event_mark(e)}{e.payee[:18]} {fmt_cents(e.amount)}")
            color = event_color(e)
            line = f'<span style="color:{color}">{text}</span>' if color else text
            lines.append(line + "&nbsp;" + mark if flagged else line)
        if len(events) > self.MAX_EVENTS:
            lines.append(f"+{len(events) - self.MAX_EVENTS} more")
        if left is not None:
            txt = html.escape(f"Left {fmt_cents(left)}")
            lines.append(f'<span style="color:{style.negative_color()}">{txt}</span>'
                         if left < 0 else txt)
        elif balance is not None:
            bal = html.escape(f"Bal {fmt_cents(balance)}")
            lines.append(f'<span style="color:{style.negative_color()}">{bal}</span>'
                         if balance < 0 else bal)
        return "<br>".join(lines)

    def _cell_plain(self, day: int, events: list, balance, *, over=(), left=None) -> str:
        shown = events[:self.MAX_EVENTS]
        flags, extra = self._split_marks(shown, over)
        lines = [str(day) + (" " + MARK_FALLBACK) * extra]
        for e, flagged in zip(shown, flags):
            text = f"{event_mark(e)}{e.payee[:18]} {fmt_cents(e.amount)}"
            lines.append(text + " " + MARK_FALLBACK if flagged else text)
        if len(events) > self.MAX_EVENTS:
            lines.append(f"+{len(events) - self.MAX_EVENTS} more")
        if left is not None:
            lines.append(f"Left {fmt_cents(left)}")
        elif balance is not None:
            lines.append(f"Bal {fmt_cents(balance)}")
        return "\n".join(lines)

    def _cell_tooltip(self, events: list, over=()) -> str:
        """A day's hover text: its events, then a line per over-limit expense
        NAMING THE CATEGORY it blew and by how much -- which is the whole point
        of the mark, since the cell itself only has room for a silhouette."""
        lines = [f"{fmt_date(e.date)}  {e.payee}  {fmt_cents(e.amount)}  ({source_label(e)})"
                 for e in events]
        for x in over:
            lines.append(
                f"Over budget: {x.category_name} -- {x.payee} {fmt_cents(x.amount_cents)} "
                f"leaves it {fmt_cents(x.over_cents)} past its limit")
        return "\n".join(lines)

    # -- budget mode ----------------------------------------------------------
    def period(self) -> str:
        """The month on screen as an ISO ``'YYYY-MM'`` budget period."""
        return f"{self.year:04d}-{self.month:02d}"

    def _burn_down(self):
        """This month's burn-down, or None when no active budget claims it.

        The budget is CHOSEN, not asked for: a household keeps one plan and the
        calendar is a glance, not a report screen. None means the panel says so
        in the summary rather than drawing an empty month -- and never opens a
        dialog, which a repaint has no business doing.

        The SPEND side honors the account selection, exactly as balance mode
        does; the allowance does not shrink with it, because a budget is a
        household plan. :meth:`_summary_text` names that asymmetry whenever the
        selection is narrower than the whole household.
        """
        if not self.budget_mode.isChecked():
            return None
        b = budgets.budget_for_period(self.conn, self.period())
        if b is None:
            self._budget_name = None
            return None
        self._budget_name = b.name
        self._budget_id = b.id
        return budgets.burn_down(
            self.conn, b.id, self.period(),
            include_predictions=self.include_predictions.isChecked(),
            account_ids=self.account_ids(),
            today=self.today)

    def _refresh_coverage(self) -> None:
        """The "covers N% of recent spending" note beside the mode toggle.

        It lives in its own label because a QCheckBox cannot hold inline rich
        text, and it carries the mark when coverage is low -- the same shape,
        meaning the same thing: this figure is past a limit.
        """
        if self.burn is None:
            self.coverage.setText("")
            self.coverage.setToolTip("")
            return
        pct = format(self.burn.coverage_pct, ".0f")
        text = html.escape(f"covers {pct}% of recent spending")
        tip = (f"{pct}% of the last {budgets.COVERAGE_DAYS} days of spending falls in "
               "categories this budget has a line for.")
        if self.burn.low_coverage:
            text = mark_html() + "&nbsp;" + text
            gaps = budgets.coverage_gaps(self.conn, self.burn.budget_id, as_of=self.today)
            if gaps:
                tip += ("\nThe biggest categories it does not cover:\n"
                        + "\n".join(f"  {name}  {fmt_cents(cents)}" for name, cents in gaps))
        self.coverage.setText(f'<span style="color:{style.muted_color()}">{text}</span>')
        self.coverage.setToolTip(tip)

    def refresh(self) -> None:
        self._stale = False
        if self._follow_clock:
            self.today = _today()
        start, end = projection.month_range(self.year, self.month)
        ids = self.account_ids()
        self.projection = projection.project(
            self.conn, ids, start, end,
            include_predictions=self.include_predictions.isChecked(), today=self.today)
        by_day = {d.date: d for d in self.projection.days}
        self.burn = self._burn_down()
        burn_by_day = {d.date: d for d in self.burn.days} if self.burn is not None else {}
        self.title.setText(_dt.date(self.year, self.month, 1).strftime("%B %Y"))
        self.grid.clearContents()
        self._plain, self._html, self._tips = {}, {}, {}
        self._events, self._marks = {}, {}
        first = _dt.date(self.year, self.month, 1)
        offset = (first.weekday() + 1) % 7
        days_in_month = int(end[8:10])
        for day in range(1, days_in_month + 1):
            iso = self._iso(day)
            d = by_day.get(iso)
            events = list(d.events) if d else []
            balance = d.balance if d is not None else None
            bd = burn_by_day.get(iso)
            over = [x for x in bd.expenses if x.over] if bd is not None else []
            left = bd.remaining_cents if bd is not None else None
            self._events[day] = events
            self._marks[day] = over
            self._plain[day] = self._cell_plain(day, events, balance, over=over, left=left)
            self._html[day] = self._cell_html(day, events, balance, over=over, left=left)
            self._tips[day] = self._cell_tooltip(events, over)
            label = QLabel(self._html[day])
            label.setTextFormat(Qt.RichText)
            label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            label.setWordWrap(True)
            label.setMargin(3)
            if self._tips[day]:
                label.setToolTip(self._tips[day])
            if iso == self.today:
                # The highlight must come from the theme: a fixed light green
                # under light text made today the one unreadable day in dark mode.
                label.setAutoFillBackground(True)
                label.setStyleSheet(f"QLabel {{ background: {today_background()}; "
                                    f"color: {style.text_color()}; }}")
            label.setContextMenuPolicy(Qt.CustomContextMenu)
            label.customContextMenuRequested.connect(
                lambda pos, day=day, lab=label: self._day_menu(day, lab.mapToGlobal(pos)))
            pos = offset + day - 1
            self.grid.setCellWidget(pos // 7, pos % 7, label)
        i = 1 if _dark() else 0
        self.legend.setText(
            f'<span style="color:{_COLORS["scheduled_out"][i]}">■ scheduled payment</span>&nbsp;&nbsp; '
            f'<span style="color:{_COLORS["scheduled_in"][i]}">■ scheduled deposit</span>&nbsp;&nbsp; '
            f'<span style="color:{_COLORS["predicted_out"][i]}">■ predicted payment</span>&nbsp;&nbsp; '
            f'<span style="color:{_COLORS["predicted_in"][i]}">■ predicted deposit</span>&nbsp;&nbsp; '
            f'<span style="color:{style.muted_color()}">■ pending pre-entry</span>'
            "&nbsp;&nbsp; (· not yet entered, ~ estimate; right-click a day to dismiss "
            "or schedule a prediction)")
        if self.budget_mode.isChecked():
            self.legend.setText(self.legend.text() + "&nbsp;&nbsp; " + mark_html()
                                + " past its category's budget")
        self.summary.setText(self._summary_text())
        self._refresh_coverage()
        self._refresh_chart_band(ids)

    def _summary_text(self) -> str:
        """The line under the grid: balances, or the month's envelope.

        Budget mode still ends with the projected low, because burn-down IGNORES
        income -- a month can be comfortably inside its envelope and still run
        the checking account dry, and that is exactly the pairing the user has
        to see to act on either number.

        When the selection is a strict SUBSET of the spending accounts, budget
        mode says so in words: the allowance is still the whole household's, so
        "Left" is not money left over -- it is what the plan has left after
        these accounts alone. Unsaid, that reads as good news.
        """
        p = self.projection
        low = f"Lowest {fmt_cents(p.low)} on {fmt_date(p.low_date)}"
        if not self.budget_mode.isChecked():
            return (f"{self.slots.describe()}:   Opening {fmt_cents(p.opening)}   "
                    f"Closing {fmt_cents(p.closing)}   {low}")
        b = self.burn
        if b is None:
            return ("No active budget covers this month -- set one up in Budget "
                    f"Planner, or switch back to balances.   {low}")
        return (f"{self._budget_name}:   Allowance {fmt_cents(b.allowance_cents)}   "
                f"Spent {fmt_cents(b.spent_cents)}   "
                f"Scheduled {fmt_cents(b.committed_cents + b.predicted_cents)}   "
                f"Left {fmt_cents(b.remaining_cents)}   |   {low}"
                f"{self._subset_note()}")

    def _subset_note(self) -> str:
        """The sentence budget mode adds when the slots narrow the spend side.

        Empty when the selection covers every spending account, which is the
        ordinary case and needs no caveat.
        """
        chosen = set(self.account_ids())
        if not chosen or not (chosen < set(self.spending_account_ids())):
            return ""
        return (f"   |   Allowance is the whole household's; spend counted only "
                f"for {self.slots.describe()}.")

    # -- the spending trend chart ---------------------------------------------
    def _spending_window(self) -> tuple:
        """The trailing 12 months ending with the month on screen, as ISO
        ``(start, end)``. Stepping the calendar walks this window too, so the
        chart always sits under the month you are looking at."""
        end = projection.month_range(self.year, self.month)[1]
        # First day of the month 11 months before the one on screen.
        total = self.year * 12 + (self.month - 1) - 11
        sy, sm = divmod(total, 12)
        return f"{sy:04d}-{sm + 1:02d}-01", end

    def _refresh_chart_band(self, account_ids) -> None:
        """Refill the band under the calendar for the mode we are in.

        Budget mode puts a bar per budget item there; every other time it is the
        trailing-year income/spending chart. A mode with nothing to show falls
        back to the chart rather than blanking the band -- budget mode with no
        budget covering this month has no envelopes to draw.
        """
        widget = self._budget_bars() if self.burn is not None else None
        if widget is None:
            widget = self._spending_canvas(account_ids)
        if widget is None:
            return          # nothing built: leave the band as the user found it
        if self.chart_widget is not None:
            self.chart_box.removeWidget(self.chart_widget)
            self.chart_widget.setParent(None)
            self.chart_widget.deleteLater()
        widget.setMinimumHeight(180)
        self.chart_box.addWidget(widget)
        self.chart_widget = widget

    def _budget_bars(self):
        """The per-item bar grid for the month's envelopes, or None.

        The figures are the DOMAIN's -- ``burn.per_category``, already narrowed
        by the account slots and the prediction checkbox and already in
        :mod:`mammon.budgets`' fixed, month-independent order (by category
        display path), which this view does not re-sort -- so a bar stays put as
        the user pages months. The SET is month-independent as well: it is the
        budget's whole item set, so no category drops out of a month it has no
        line for -- it shows a zero allowance instead. Nothing about an envelope
        is computed, filtered or re-ordered here.
        """
        try:
            from mammon.ui.budget_bars import BudgetBarGrid
        except Exception:
            return None
        try:
            return BudgetBarGrid(self.burn.per_category)
        except Exception:
            return None

    def _spending_canvas(self, account_ids):
        """The income/spending bar chart for the current accounts and window, or None.

        The aggregation (money-in and money-out per month) is pure and lives in
        :mod:`mammon.reports.charts`; here we only render it. matplotlib is a
        hard dependency, but it is imported lazily -- and a failure to import or
        draw degrades to no chart rather than taking the home page down."""
        try:
            from mammon.reports.charts import spending_by_period
            from mammon.ui.charts import SpendingBarCanvas
        except Exception:
            return None
        start, end = self._spending_window()
        try:
            report = spending_by_period(self.conn, start, end,
                                        account_ids=account_ids)
            return SpendingBarCanvas(report)
        except Exception:
            return None

    # -- the right-click menu on a day ----------------------------------------
    def _day_menu(self, day: int, global_pos) -> None:
        menu = QMenu(self)
        actions = []
        for e in self.predictions_on(day):
            actions.append((menu.addAction(f"Dismiss prediction: {e.payee}"),
                            lambda e=e: self.dismiss_prediction(e.account_id, e.payee)))
            actions.append((menu.addAction(f"Schedule {e.payee}…"),
                            lambda e=e: self.schedule_prediction(e)))
        if actions:
            menu.addSeparator()
        if predictions.dismissed(self.conn):
            actions.append((menu.addAction("Dismissed predictions…"), self.show_dismissed))
        if not actions:
            return
        chosen = menu.exec_(global_pos)
        for act, fn in actions:
            if chosen is act:
                fn()
                return

    def dismiss_prediction(self, account_id: int, payee: str) -> None:
        predictions.dismiss(self.conn, account_id, payee)
        self.refresh()

    def _edit_definition(self, entry: dict) -> Optional[dict]:
        """Open the scheduled-payment editor pre-filled from a prediction and
        return its values, or None when canceled (a seam tests override)."""
        from mammon.ui.scheduled_payments_dialog import ScheduledPaymentEditor
        # learn_splits=True: this flow (schedule_prediction below) actually
        # learns the payee's split onto the definition, so the editor surfaces
        # the inherited split/category the user is about to accept.
        dlg = ScheduledPaymentEditor(self.conn, entry=entry, parent=self,
                                     learn_splits=True)
        if dlg.exec_() != QDialog.Accepted:
            return None
        return dlg.values()

    def schedule_prediction(self, e) -> Optional[int]:
        """Turn a predicted event into a definition (the editor lets the user
        correct the amount and date first); the prediction then stops, since
        a scheduled payee is never predicted. Returns the definition id."""
        # A payee can be several interleaved bills, so it can have several
        # predictions on one account (predictions.split_substreams): match the
        # event's amount too, and only fall back to the payee alone.
        cands = [p for p in predictions.predict_recurring(
            self.conn, self.today, account_ids=[e.account_id], include_dismissed=True)
            if p.key == e.payee_key]
        p = next((p for p in cands if p.amount == e.amount), None) or \
            (cands[0] if cands else None)
        entry = p.entry() if p is not None else {
            "account_id": e.account_id, "payee": e.payee, "amount": e.amount,
            "frequency": "monthly", "next_date": e.date, "category_id": None}
        entry["next_date"] = e.date
        entry["category_label"] = ledger.category_path(self.conn, entry.get("category_id"))
        values = self._edit_definition(entry)
        if values is None:
            return None
        # If the predicted payee's history is split, learn that breakdown (the
        # same lines the register's "Copy from previous <payee> split" copies)
        # and persist it on the definition, so every pre-entry reproduces it.
        # A transfer definition is a single whole-transaction move -- no split.
        if values.get("transfer_account_id") is None:
            learned = ledger.previous_split_for_payee(self.conn, values.get("payee"))
            if len(learned) >= 2:
                values["splits"] = learned
        sid = scheduled.add_scheduled(self.conn, **values)
        self.refresh()
        return sid

    def show_dismissed(self) -> None:
        DismissedPredictionsDialog(self.conn, parent=self).exec_()
        self.refresh()

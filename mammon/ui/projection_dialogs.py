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

The calendar colours each event by what it is, in both themes: a scheduled
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

Neither of them writes anything except a prediction dismissal or, through the
scheduled-payment editor, a new definition.
"""
from __future__ import annotations

import datetime as _dt
import html
from typing import Optional

from PyQt5.QtCore import QDate, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu, QPushButton,
    QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from mammon import ledger, predictions, projection, scheduled
from mammon.ui import prefs, style
from mammon.ui.models import fmt_cents, fmt_date

ALL_SPENDING = "All spending accounts"
SPENDING_TYPES = predictions.SPENDING_TYPES

# Event colours by theme: (light, dark).
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


def _today() -> str:
    return QDate.currentDate().toString("yyyy-MM-dd")


def _dark() -> bool:
    return style.theme() == "dark"


def event_color(e) -> Optional[str]:
    """The colour an event is drawn in, for the active theme; None for an
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
    combo = QComboBox()
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
        self.horizon = QComboBox()
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
        self._plain: dict = {}
        self._events: dict = {}
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
        top.addWidget(self.prev_btn)
        top.addWidget(self.title, 0, Qt.AlignCenter)
        top.addWidget(self.next_btn)
        top.addSpacing(16)
        self.include_predictions = _predictions_box()
        top.addWidget(self.include_predictions)

        # The account slots: which accounts this month is summed over.
        slots_row = QHBoxLayout()
        slots_row.addWidget(QLabel("Accounts"))
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

        # A spending-per-month bar chart under the calendar. The grid answers
        # "what is coming"; this answers "how has spending trended" -- the two
        # glances a home page owes. It lives in its own box so refresh() can
        # swap the throwaway matplotlib canvas without disturbing the calendar.
        self.chart_box = QVBoxLayout()
        self.spending_chart = None

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
        every spending account."""
        return self.slots.account_ids() or [int(a["id"]) for a in spending_accounts(self.conn)]

    # -- navigation --------------------------------------------------------
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

    def events_on(self, day: int) -> list:
        return list(self._events.get(int(day), []))

    def predictions_on(self, day: int) -> list:
        return [e for e in self.events_on(day) if e.source == projection.PREDICTED]

    def _iso(self, day: int) -> str:
        return f"{self.year:04d}-{self.month:02d}-{int(day):02d}"

    # -- drawing --------------------------------------------------------------
    def _cell_html(self, day: int, events: list, balance) -> str:
        i = 1 if _dark() else 0
        lines = [f"<b>{day}</b>"]
        for e in events[:self.MAX_EVENTS]:
            text = html.escape(f"{event_mark(e)}{e.payee[:18]} {fmt_cents(e.amount)}")
            color = event_color(e)
            lines.append(f'<span style="color:{color}">{text}</span>' if color else text)
        if len(events) > self.MAX_EVENTS:
            lines.append(f"+{len(events) - self.MAX_EVENTS} more")
        if balance is not None:
            bal = html.escape(f"Bal {fmt_cents(balance)}")
            lines.append(f'<span style="color:{style.negative_color()}">{bal}</span>'
                         if balance < 0 else bal)
        return "<br>".join(lines)

    def _cell_plain(self, day: int, events: list, balance) -> str:
        lines = [str(day)]
        for e in events[:self.MAX_EVENTS]:
            lines.append(f"{event_mark(e)}{e.payee[:18]} {fmt_cents(e.amount)}")
        if len(events) > self.MAX_EVENTS:
            lines.append(f"+{len(events) - self.MAX_EVENTS} more")
        if balance is not None:
            lines.append(f"Bal {fmt_cents(balance)}")
        return "\n".join(lines)

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
        self.title.setText(_dt.date(self.year, self.month, 1).strftime("%B %Y"))
        self.grid.clearContents()
        self._plain, self._events = {}, {}
        first = _dt.date(self.year, self.month, 1)
        offset = (first.weekday() + 1) % 7
        days_in_month = int(end[8:10])
        for day in range(1, days_in_month + 1):
            iso = self._iso(day)
            d = by_day.get(iso)
            events = list(d.events) if d else []
            balance = d.balance if d is not None else None
            self._events[day] = events
            self._plain[day] = self._cell_plain(day, events, balance)
            label = QLabel(self._cell_html(day, events, balance))
            label.setTextFormat(Qt.RichText)
            label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            label.setWordWrap(True)
            label.setMargin(3)
            if events:
                label.setToolTip("\n".join(
                    f"{fmt_date(e.date)}  {e.payee}  {fmt_cents(e.amount)}  ({source_label(e)})"
                    for e in events))
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
        p = self.projection
        self.summary.setText(
            f"{self.slots.describe()}:   Opening {fmt_cents(p.opening)}   "
            f"Closing {fmt_cents(p.closing)}   Lowest {fmt_cents(p.low)} on "
            f"{fmt_date(p.low_date)}")
        self._refresh_spending_chart(ids)

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

    def _refresh_spending_chart(self, account_ids) -> None:
        """Rebuild the income/spending bar chart for the current accounts and window.

        The aggregation (money-in and money-out per month) is pure and lives in
        :mod:`mammon.reports.charts`; here we only render it. matplotlib is a
        hard dependency, but it is imported lazily -- and a failure to import or
        draw degrades to no chart rather than taking the home page down."""
        try:
            from mammon.reports.charts import spending_by_period
            from mammon.ui.charts import SpendingBarCanvas
        except Exception:
            return
        start, end = self._spending_window()
        try:
            report = spending_by_period(self.conn, start, end,
                                        account_ids=account_ids)
            canvas = SpendingBarCanvas(report)
        except Exception:
            return
        if self.spending_chart is not None:
            self.chart_box.removeWidget(self.spending_chart)
            self.spending_chart.setParent(None)
            self.spending_chart.deleteLater()
        canvas.setMinimumHeight(180)
        self.chart_box.addWidget(canvas)
        self.spending_chart = canvas

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
        return its values, or None when cancelled (a seam tests override)."""
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
        p = next((p for p in predictions.predict_recurring(
            self.conn, self.today, account_ids=[e.account_id], include_dismissed=True)
            if p.key == e.payee_key), None)
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
